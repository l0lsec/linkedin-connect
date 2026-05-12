#!/usr/bin/env python3
"""
Bulk-find LinkedIn profile URLs for employees of a given company.

Reads a CSV with columns (First Name, Last Name, Job Title, Level, Region,
Linkedin Links), runs a SerpAPI Google search for each pending row, keeps
results whose title/snippet mentions the target company, and writes the
matching linkedin.com/in/<slug> URLs (joined with " | ") into column F.

Progress is checkpointed to a sidecar JSON file so the job is fully
resumable across crashes, rate-limits, or Ctrl-C.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

SERPAPI_URL = "https://serpapi.com/search.json"

LINKEDIN_IN_RE = re.compile(
    r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([^/?#]+)", re.I
)

MAX_MATCHES_PER_ROW = 5
BACKOFF_SCHEDULE = (2, 4, 8, 16, 32)
RATE_LIMIT_429_SLEEP = 90
RATE_LIMIT_429_MAX_ATTEMPTS = 30
PROGRESS_SCHEMA_VERSION = 2


def _derive_paths(csv_path: Path) -> tuple[Path, Path, Path]:
    """Derive progress/sidecar file paths from the input CSV stem."""
    stem = csv_path.stem
    parent = csv_path.parent
    progress = parent / f"{stem}.progress.json"
    no_match = parent / f"{stem}_no_match.txt"
    ambiguous = parent / f"{stem}_ambiguous.txt"
    return progress, no_match, ambiguous


def _build_company_regex(company: str, aliases: list[str]) -> re.Pattern[str]:
    """Build a compiled regex that matches the company name or any alias."""
    terms = [re.escape(company)]
    for alias in aliases:
        terms.append(re.escape(alias))
    pattern = r"\b(" + "|".join(terms) + r")\b"
    return re.compile(pattern, re.I)


def _build_search_filter(company: str, aliases: list[str]) -> str:
    """Build the parenthesized OR clause for the SerpAPI query."""
    terms = set()
    terms.add(company)
    for alias in aliases:
        terms.add(alias)
    quoted = [f'"{t}"' if " " in t else t for t in sorted(terms)]
    return "(" + " OR ".join(quoted) + ")"


def name_key(first: str, last: str) -> str:
    """Normalize (first, last) into a stable progress-cache key."""
    norm = f"{first} {last}".lower()
    return re.sub(r"\s+", " ", norm).strip()


class GlobalRateLimiter:
    """Simple sliding-window limiter: at most `max_per_window` calls per `window` seconds."""

    def __init__(self, max_per_window: int, window: float = 60.0):
        self.max_per_window = max_per_window
        self.window = window
        self._lock = threading.Lock()
        self._calls: list[float] = []

    def acquire(self) -> None:
        if self.max_per_window <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - self.window
                self._calls = [t for t in self._calls if t > cutoff]
                if len(self._calls) < self.max_per_window:
                    self._calls.append(now)
                    return
                wait = self._calls[0] + self.window - now + 0.05
            time.sleep(max(wait, 0.05))


_rate_limiter: GlobalRateLimiter | None = None


# ---------------------------------------------------------------------------
# Progress persistence
# ---------------------------------------------------------------------------

def load_progress(progress_path: Path) -> dict[str, Any]:
    if progress_path.exists():
        try:
            data = json.loads(progress_path.read_text(encoding="utf-8"))
            data.setdefault("completed", {})
            data.setdefault("errors", {})
            data.setdefault("schema_version", 1)
            return data
        except (json.JSONDecodeError, ValueError):
            pass
    return {"schema_version": PROGRESS_SCHEMA_VERSION, "completed": {}, "errors": {}}


def migrate_progress_to_name_keys(
    progress: dict[str, Any], rows: list[list[str]]
) -> bool:
    """Migrate progress entries from row-index keys (v1) to name keys (v2).

    Returns True if migration occurred (caller should re-flush). Idempotent:
    if already at schema v2 (or all keys look like names already), returns False.
    """
    if progress.get("schema_version", 1) >= PROGRESS_SCHEMA_VERSION:
        return False

    def looks_like_int_key(k: str) -> bool:
        return k.isdigit()

    completed_old: dict[str, Any] = progress.get("completed", {})
    errors_old: dict[str, Any] = progress.get("errors", {})

    has_int_keys = any(looks_like_int_key(k) for k in completed_old) or any(
        looks_like_int_key(k) for k in errors_old
    )
    if not has_int_keys:
        progress["schema_version"] = PROGRESS_SCHEMA_VERSION
        return True

    completed_new: dict[str, list[str]] = {}
    errors_new: dict[str, str] = {}
    dropped = 0
    for k, v in completed_old.items():
        if looks_like_int_key(k):
            idx = int(k)
            if 0 <= idx < len(rows):
                completed_new[name_key(rows[idx][0], rows[idx][1])] = v
            else:
                dropped += 1
        else:
            completed_new[k] = v
    for k, v in errors_old.items():
        if looks_like_int_key(k):
            idx = int(k)
            if 0 <= idx < len(rows):
                errors_new[name_key(rows[idx][0], rows[idx][1])] = v
            else:
                dropped += 1
        else:
            errors_new[k] = v

    progress["completed"] = completed_new
    progress["errors"] = errors_new
    progress["schema_version"] = PROGRESS_SCHEMA_VERSION
    if dropped:
        print(f"  [migrate] dropped {dropped} progress entries with out-of-range indices")
    print(
        f"  [migrate] progress.json upgraded to schema v{PROGRESS_SCHEMA_VERSION} "
        f"(keyed by name): {len(completed_new)} completed, {len(errors_new)} errors"
    )
    return True


def save_progress_atomic(progress: dict[str, Any], progress_path: Path) -> None:
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(progress_path.parent),
        prefix=progress_path.name + ".",
        suffix=".tmp",
    )
    try:
        json.dump(progress, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, progress_path)
    finally:
        if os.path.exists(tmp.name):
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


def regenerate_sidecars(
    progress: dict[str, Any],
    rows: list[list[str]],
    no_match_path: Path,
    ambiguous_path: Path,
) -> None:
    """Rewrite no-match and ambiguous sidecar files deterministically from
    progress state. Overwrites (does not append) so re-runs are idempotent.
    """
    completed: dict[str, list[str]] = progress.get("completed", {})
    name_to_indices: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        name_to_indices.setdefault(name_key(row[0], row[1]), []).append(i)

    no_match_rows: list[tuple[int, str, str]] = []
    ambig_rows: list[tuple[int, str, str, list[str]]] = []
    for key, urls in completed.items():
        indices = name_to_indices.get(key, [])
        for idx in indices:
            if not (0 <= idx < len(rows)):
                continue
            first = rows[idx][0].strip()
            last = rows[idx][1].strip()
            if not urls:
                no_match_rows.append((idx, first, last))
            elif len(urls) > 1:
                ambig_rows.append((idx, first, last, urls))

    no_match_rows.sort(key=lambda t: t[0])
    ambig_rows.sort(key=lambda t: t[0])

    no_match_path.write_text(
        "\n".join(f"{i}\t{f} {l}" for i, f, l in no_match_rows) + ("\n" if no_match_rows else ""),
        encoding="utf-8",
    )
    ambiguous_path.write_text(
        "\n".join(f"{i}\t{f} {l}\t" + " | ".join(u) for i, f, l, u in ambig_rows)
        + ("\n" if ambig_rows else ""),
        encoding="utf-8",
    )


def save_csv_atomic(csv_path: Path, headers: list[str], rows: list[list[str]]) -> None:
    parent = csv_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        delete=False,
        dir=str(parent),
        prefix=csv_path.name + ".",
        suffix=".tmp",
    )
    try:
        writer = csv.writer(tmp, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(headers)
        writer.writerows(rows)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, csv_path)
    finally:
        if os.path.exists(tmp.name):
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# SerpAPI client
# ---------------------------------------------------------------------------

def serpapi_get(query: str, api_key: str, *, timeout: int = 30) -> dict[str, Any]:
    """GET SerpAPI with exponential backoff on 429/5xx. Raises on permanent failure."""
    params = {
        "engine": "google",
        "q": query,
        "num": "10",
        "api_key": api_key,
        "hl": "en",
        "gl": "us",
    }
    url = f"{SERPAPI_URL}?{urllib.parse.urlencode(params)}"

    last_err: Exception | None = None
    rate_429_attempts = 0
    server_attempts = 0
    while True:
        if _rate_limiter is not None:
            _rate_limiter.acquire()
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", "linkedin-finder/1.0")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code == 429:
                rate_429_attempts += 1
                if rate_429_attempts > RATE_LIMIT_429_MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"SerpAPI 429 persists after {rate_429_attempts} attempts"
                    ) from exc
                time.sleep(RATE_LIMIT_429_SLEEP)
                continue
            if exc.code in (500, 502, 503, 504):
                server_attempts += 1
                if server_attempts > len(BACKOFF_SCHEDULE):
                    raise RuntimeError(f"SerpAPI HTTP {exc.code}: persistent server error") from exc
                time.sleep(BACKOFF_SCHEDULE[server_attempts - 1])
                continue
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            raise RuntimeError(f"SerpAPI HTTP {exc.code}: {body[:200]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
            server_attempts += 1
            if server_attempts > len(BACKOFF_SCHEDULE):
                raise RuntimeError(f"SerpAPI gave up after retries: {last_err}") from exc
            time.sleep(BACKOFF_SCHEDULE[server_attempts - 1])
            continue


# ---------------------------------------------------------------------------
# Result extraction
# ---------------------------------------------------------------------------

def _result_text(r: dict[str, Any]) -> str:
    parts: list[str] = []
    for k in ("title", "snippet", "displayed_link"):
        v = r.get(k)
        if isinstance(v, str):
            parts.append(v)
    rich = r.get("rich_snippet")
    if isinstance(rich, dict):
        parts.append(json.dumps(rich))
    return " ".join(parts)


def _normalize_linkedin(url: str) -> tuple[str, str] | None:
    """Return (canonical_url, lowercase_slug) or None if not a linkedin /in/ URL."""
    m = LINKEDIN_IN_RE.match(url.strip())
    if not m:
        return None
    slug = m.group(1).rstrip("/")
    return f"https://www.linkedin.com/in/{slug}", slug.lower()


def extract_company_matches(
    data: dict[str, Any], company_re: re.Pattern[str]
) -> list[str]:
    """Return list of canonical linkedin.com/in/<slug> URLs whose result text
    mentions the target company. Deduped, capped at MAX_MATCHES_PER_ROW."""
    out: list[str] = []
    seen: set[str] = set()
    for r in data.get("organic_results") or []:
        link = r.get("link")
        if not isinstance(link, str):
            continue
        norm = _normalize_linkedin(link)
        if not norm:
            continue
        canonical, slug_key = norm
        if slug_key in seen:
            continue
        text = _result_text(r)
        if not company_re.search(text):
            continue
        seen.add(slug_key)
        out.append(canonical)
        if len(out) >= MAX_MATCHES_PER_ROW:
            break
    return out


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------

def build_query(
    first: str,
    last: str,
    *,
    with_company_filter: bool,
    company_filter: str = "",
) -> str:
    name = f'"{first.strip()} {last.strip()}"'
    if with_company_filter and company_filter:
        return f"site:linkedin.com/in {name} {company_filter}"
    return f"site:linkedin.com/in {name}"


def process_row(
    idx: int,
    first: str,
    last: str,
    api_key: str,
    company_re: re.Pattern[str],
    company_filter: str,
) -> tuple[int, list[str], str | None]:
    """Search SerpAPI for one row. Returns (idx, matches, error_message)."""
    try:
        data = serpapi_get(
            build_query(first, last, with_company_filter=True, company_filter=company_filter),
            api_key,
        )
        matches = extract_company_matches(data, company_re)
        if not matches:
            data = serpapi_get(
                build_query(first, last, with_company_filter=False),
                api_key,
            )
            matches = extract_company_matches(data, company_re)
        return idx, matches, None
    except Exception as exc:  # noqa: BLE001
        return idx, [], str(exc)


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

def load_csv(csv_path: Path) -> tuple[list[str], list[list[str]]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    while len(headers) < 6:
        headers.append("")
    for row in rows:
        while len(row) < 6:
            row.append("")
    return headers, rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Find LinkedIn profiles for employees of a given company via SerpAPI."
    )
    ap.add_argument("--csv", type=Path, required=True, help="Path to the input CSV file")
    ap.add_argument(
        "--company",
        required=True,
        help="Primary company name to match in search results (e.g. 'Google')",
    )
    ap.add_argument(
        "--company-aliases",
        nargs="*",
        default=[],
        help="Additional names/abbreviations for the company (e.g. 'Alphabet' 'GOOG')",
    )
    ap.add_argument(
        "--api-key",
        default=os.environ.get("SERPAPI_KEY"),
        help="SerpAPI key (or set SERPAPI_KEY env var)",
    )
    ap.add_argument("--start", type=int, default=0, help="Start at row index (0-based, excludes header)")
    ap.add_argument("--limit", type=int, default=None, help="Process at most N pending rows")
    ap.add_argument("--batch-size", type=int, default=50, help="Flush CSV+progress every N completions")
    ap.add_argument("--workers", type=int, default=5, help="Parallel SerpAPI workers")
    ap.add_argument("--dry-run", action="store_true", help="Do not write CSV; print decisions")
    ap.add_argument(
        "--retry-no-match",
        action="store_true",
        help="Re-process rows previously marked completed with zero matches",
    )
    ap.add_argument(
        "--retry-errors",
        action="store_true",
        help="Re-process rows that previously failed with an error (default: skip them on rerun)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Bypass progress + CSV caches and re-process every row from scratch",
    )
    ap.add_argument(
        "--max-per-minute",
        type=int,
        default=45,
        help="Global cap on SerpAPI requests per rolling 60s window (default 45)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global _rate_limiter
    args = parse_args(argv)
    if not args.api_key:
        print("ERROR: --api-key or SERPAPI_KEY env var required", file=sys.stderr)
        return 2
    if not args.csv.is_file():
        print(f"ERROR: CSV not found: {args.csv}", file=sys.stderr)
        return 1

    company_re = _build_company_regex(args.company, args.company_aliases)
    company_filter = _build_search_filter(args.company, args.company_aliases)
    progress_path, no_match_path, ambiguous_path = _derive_paths(args.csv)

    _rate_limiter = GlobalRateLimiter(max_per_window=args.max_per_minute, window=60.0)

    headers, rows = load_csv(args.csv)
    progress = load_progress(progress_path)
    migrated = migrate_progress_to_name_keys(progress, rows)
    completed: dict[str, list[str]] = progress["completed"]
    errors: dict[str, str] = progress["errors"]

    if args.force:
        for row in rows:
            row[5] = ""
        completed.clear()
        errors.clear()
    else:
        for idx, row in enumerate(rows):
            k = name_key(row[0], row[1])
            urls = completed.get(k)
            if isinstance(urls, list) and urls and not row[5].strip():
                row[5] = " | ".join(urls)

    pending: list[tuple[int, str, str, str]] = []
    skipped_filled = 0
    skipped_completed = 0
    skipped_error = 0
    for idx, row in enumerate(rows):
        if idx < args.start:
            continue
        first = row[0].strip()
        last = row[1].strip()
        if not first and not last:
            continue
        k = name_key(first, last)
        if rows[idx][5].strip() and not args.force:
            skipped_filled += 1
            continue
        if not args.force and k in completed:
            if completed[k]:
                skipped_completed += 1
                continue
            if not args.retry_no_match:
                skipped_completed += 1
                continue
        if not args.force and k in errors and not args.retry_errors:
            skipped_error += 1
            continue
        pending.append((idx, first, last, k))
        if args.limit is not None and len(pending) >= args.limit:
            break

    total_rows = len(rows)
    print(
        f"CSV: {args.csv} | Company: {args.company}\n"
        f"Rows: {total_rows} | filled: {skipped_filled} | "
        f"completed-cache: {skipped_completed} | errors-cache: {skipped_error} | "
        f"pending this run: {len(pending)}\n"
        f"Workers: {args.workers} | Batch: {args.batch_size} | "
        f"Dry-run: {args.dry_run} | Force: {args.force} | "
        f"Retry no-match: {args.retry_no_match} | Retry errors: {args.retry_errors}"
    )

    if not pending:
        if migrated and not args.dry_run:
            save_progress_atomic(progress, progress_path)
            regenerate_sidecars(progress, rows, no_match_path, ambiguous_path)
            print("Migrated progress + sidecars refreshed.")
        print("Nothing to do.")
        return 0

    flush_lock = threading.Lock()
    processed_since_flush = 0
    total_processed = 0
    total_match = 0
    total_no_match = 0
    total_error = 0

    def flush() -> None:
        with flush_lock:
            if not args.dry_run:
                save_csv_atomic(args.csv, headers, rows)
            save_progress_atomic(progress, progress_path)
            regenerate_sidecars(progress, rows, no_match_path, ambiguous_path)

    stop_requested = {"v": False}

    def handle_sigint(signum, frame):  # noqa: ANN001
        if stop_requested["v"]:
            print("\nSecond Ctrl-C, exiting hard.", file=sys.stderr)
            sys.exit(130)
        stop_requested["v"] = True
        print("\nCtrl-C received; finishing in-flight work and flushing...", file=sys.stderr)

    signal.signal(signal.SIGINT, handle_sigint)

    start_ts = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(
                process_row, idx, first, last, args.api_key, company_re, company_filter
            ): (idx, first, last, key)
            for idx, first, last, key in pending
        }
        for fut in as_completed(futures):
            idx, first, last, key = futures[fut]
            try:
                _, matches, err = fut.result()
            except Exception as exc:  # noqa: BLE001
                matches, err = [], str(exc)

            total_processed += 1
            if err:
                total_error += 1
                errors[key] = err
                print(f"  [{idx}] {first} {last}: ERROR {err[:120]}")
            elif matches:
                total_match += 1
                rows[idx][5] = " | ".join(matches)
                completed[key] = matches
                errors.pop(key, None)
                tag = "MATCH" if len(matches) == 1 else f"AMBIG({len(matches)})"
                print(f"  [{idx}] {first} {last}: {tag} {matches[0]}")
            else:
                total_no_match += 1
                completed[key] = []
                errors.pop(key, None)
                print(f"  [{idx}] {first} {last}: no-match")

            processed_since_flush += 1
            if processed_since_flush >= args.batch_size:
                flush()
                processed_since_flush = 0
                elapsed = time.time() - start_ts
                rate = total_processed / max(elapsed, 1e-6)
                print(
                    f"  [flush] processed={total_processed} "
                    f"match={total_match} no_match={total_no_match} err={total_error} "
                    f"rate={rate:.2f}/s"
                )

            if stop_requested["v"]:
                for f2 in futures:
                    if not f2.done():
                        f2.cancel()
                break

    flush()
    elapsed = time.time() - start_ts
    print(
        f"\nDone. processed={total_processed} match={total_match} "
        f"no_match={total_no_match} err={total_error} elapsed={elapsed:.0f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
