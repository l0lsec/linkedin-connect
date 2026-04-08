#!/usr/bin/env python3
"""
Automate LinkedIn connection requests via the Voyager API.

Reads a text file of LinkedIn profile URLs (one per line), resolves each
public slug to an internal profile URN, then sends a connect request with
a custom note.  Progress is persisted so re-runs skip already-processed URLs.

Requires two values from your browser session (DevTools -> Application -> Cookies):
  li_at      – the main session token
  JSESSIONID – used as the CSRF token (strip surrounding quotes)
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

WEEKLY_LIMIT_DEFAULT = 150

VOYAGER_BASE = "https://www.linkedin.com/voyager/api"

CONNECT_ENDPOINT = (
    f"{VOYAGER_BASE}/voyagerRelationshipsDashMemberRelationships"
    "?action=verifyQuotaAndCreateV2"
    "&decorationId=com.linkedin.voyager.dash.deco.relationships"
    ".InvitationCreationResultWithInvitee-2"
)

PROFILE_ENDPOINT = (
    f"{VOYAGER_BASE}/identity/dash/profiles"
    "?q=memberIdentity&memberIdentity={{SLUG}}"
    "&decorationId=com.linkedin.voyager.dash.deco.identity.profile"
    ".WebTopCardCore-19"
)

SLUG_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.I)
URN_RE = re.compile(r"urn:li:fsd_profile:([A-Za-z0-9_-]+)")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Networking helpers (stdlib only)
# ---------------------------------------------------------------------------

def _common_headers(csrf: str) -> dict[str, str]:
    return {
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "accept-language": "en-US,en;q=0.9",
        "csrf-token": csrf,
        "user-agent": USER_AGENT,
        "x-li-lang": "en_US",
        "x-li-deco-include-micro-schema": "true",
        "x-restli-protocol-version": "2.0.0",
        "x-li-track": json.dumps({
            "clientVersion": "1.13.43366",
            "mpVersion": "1.13.43366",
            "osName": "web",
            "timezoneOffset": -4,
            "timezone": "America/New_York",
            "deviceFormFactor": "DESKTOP",
            "mpName": "voyager-web",
            "displayDensity": 1,
            "displayWidth": 1920,
            "displayHeight": 1080,
        }),
    }


def _cookie_header(li_at: str, csrf: str) -> str:
    return f'li_at={li_at}; JSESSIONID="{csrf}"'


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str],
    cookie: str,
    data: bytes | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any] | str]:
    """Fire an HTTP request and return (status_code, parsed_json_or_text)."""
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            return exc.code, json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return exc.code, body


# ---------------------------------------------------------------------------
# Phase 1 — load & deduplicate URLs
# ---------------------------------------------------------------------------

def load_urls(path: Path) -> list[str]:
    """Return deduplicated, normalised LinkedIn URLs from a text file."""
    seen: set[str] = set()
    urls: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or not SLUG_RE.search(line):
            continue
        m = SLUG_RE.search(line)
        slug = m.group(1).rstrip("/").lower() if m else ""
        if not slug or slug in seen:
            continue
        seen.add(slug)
        urls.append(f"https://www.linkedin.com/in/{slug}")
    return urls


def load_progress(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if "send_log" not in data:
                data["send_log"] = []
            return data
        except (json.JSONDecodeError, ValueError):
            pass
    return {"sent": [], "failed": {}, "send_log": [], "last_run": None}


def save_progress(path: Path, progress: dict[str, Any]) -> None:
    progress["last_run"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(progress, indent=2), encoding="utf-8")


def _start_of_week_utc() -> datetime:
    """Return midnight UTC of the most recent Monday."""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def count_sends_this_week(progress: dict[str, Any]) -> int:
    """Count entries in send_log whose timestamp falls in the current Mon-Sun week."""
    cutoff = _start_of_week_utc().isoformat()
    return sum(1 for entry in progress.get("send_log", []) if entry.get("ts", "") >= cutoff)


def record_send(progress: dict[str, Any], url: str) -> None:
    """Append a timestamped entry to the send log."""
    progress["send_log"].append({
        "url": url,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Phase 2 — resolve slug → URN
# ---------------------------------------------------------------------------

def resolve_urn(
    slug: str, *, headers: dict[str, str], cookie: str
) -> str | None:
    """GET the profile identity endpoint and extract the fsd_profile URN."""
    url = PROFILE_ENDPOINT.replace("{{SLUG}}", slug)
    status, body = _request(url, headers=headers, cookie=cookie)
    if status != 200:
        return None
    if isinstance(body, dict):
        text = json.dumps(body)
    else:
        text = body
    m = URN_RE.search(text)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Phase 3 — send connection request
# ---------------------------------------------------------------------------

def send_connect(
    profile_urn: str,
    message: str,
    *,
    headers: dict[str, str],
    cookie: str,
) -> tuple[int, str]:
    """POST a connection invitation.  Returns (http_status, short_reason)."""
    payload = json.dumps({
        "invitee": {
            "inviteeUnion": {
                "memberProfile": profile_urn,
            }
        },
        "customMessage": message,
    }).encode("utf-8")

    h = dict(headers)
    h["content-type"] = "application/json; charset=UTF-8"
    h["origin"] = "https://www.linkedin.com"

    status, body = _request(
        CONNECT_ENDPOINT, method="POST", headers=h, cookie=cookie, data=payload
    )

    if status == 200:
        return status, "sent"
    if status == 429:
        return status, "rate-limited"
    if status == 400:
        return status, "already connected or pending invite"
    reason = ""
    if isinstance(body, dict):
        reason = body.get("message", "") or body.get("status", "")
    return status, str(reason) or f"HTTP {status}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Send LinkedIn connection requests from a list of profile URLs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 linkedin_connect.py \\\n"
            '    --urls linkedins.txt \\\n'
            '    --cookie "AQEDASEGVo..." \\\n'
            '    --csrf "ajax:106700709..." \\\n'
            '    --message "Connecting from XXX..." \\\n'
            "    --daily-limit 25 --dry-run"
        ),
    )
    ap.add_argument("--urls", type=Path, required=True, help="Text file with one LinkedIn URL per line")
    ap.add_argument("--message", default="Connecting from XXX...", help="Note attached to connection request")
    ap.add_argument("--cookie", required=True, help="li_at session cookie value")
    ap.add_argument("--csrf", required=True, help='JSESSIONID value (strip outer quotes)')
    ap.add_argument("--daily-limit", type=int, default=25, help="Max connection requests per run (default 25)")
    ap.add_argument("--weekly-limit", type=int, default=WEEKLY_LIMIT_DEFAULT, help=f"Max connection requests per Mon-Sun week (default {WEEKLY_LIMIT_DEFAULT})")
    ap.add_argument("--delay-min", type=float, default=45, help="Min seconds between requests (default 45)")
    ap.add_argument("--delay-max", type=float, default=120, help="Max seconds between requests (default 120)")
    ap.add_argument("--progress", type=Path, default=None, help="Path to progress JSON (default: <urls>.progress.json)")
    ap.add_argument("--dry-run", action="store_true", help="Resolve URNs and print results without sending invites")
    ap.add_argument("--auto", action="store_true", help="Run continuously: send daily batch, sleep overnight, repeat until all URLs are done")
    return ap.parse_args(argv)


def _seconds_until_tomorrow_8am() -> float:
    """Seconds from now until 8:00 AM local time tomorrow (plus some jitter)."""
    now = datetime.now()
    tomorrow_8am = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    jitter = random.uniform(0, 1800)
    return (tomorrow_8am - now).total_seconds() + jitter


def _seconds_until_next_monday_8am() -> float:
    """Seconds from now until 8:00 AM local time next Monday (plus some jitter)."""
    now = datetime.now()
    days_ahead = (7 - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    next_monday = (now + timedelta(days=days_ahead)).replace(hour=8, minute=0, second=0, microsecond=0)
    jitter = random.uniform(0, 1800)
    return (next_monday - now).total_seconds() + jitter


def _format_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def run_batch(
    args: argparse.Namespace,
    progress_path: Path,
    progress: dict[str, Any],
) -> str:
    """Send one daily batch. Returns a stop reason string:
      'done'          – no more pending URLs
      'daily_limit'   – daily cap hit
      'weekly_limit'  – weekly cap hit
      'rate_limited'  – LinkedIn 429
      'dry_run'       – dry-run finished
    """
    sent_set: set[str] = set(progress["sent"])
    urls = load_urls(args.urls)
    pending = [u for u in urls if u not in sent_set and u not in progress["failed"]]

    sent_this_week = count_sends_this_week(progress)
    weekly_remaining = max(0, args.weekly_limit - sent_this_week)

    print(f"\nLoaded {len(urls)} unique URLs, {len(sent_set)} already sent, {len(progress['failed'])} previously failed")
    print(f"Pending: {len(pending)}  |  Daily limit: {args.daily_limit}  |  Dry-run: {args.dry_run}")
    print(f"Weekly: {sent_this_week}/{args.weekly_limit} used  |  {weekly_remaining} remaining this week")
    print()

    if not pending:
        return "done"

    if weekly_remaining <= 0 and not args.dry_run:
        print(f"Weekly limit ({args.weekly_limit}) already reached.")
        return "weekly_limit"

    headers = _common_headers(args.csrf)
    cookie = _cookie_header(args.cookie, args.csrf)

    sent_this_run = 0
    stop_reason = "done"

    for i, url in enumerate(pending):
        if sent_this_run >= args.daily_limit:
            stop_reason = "daily_limit"
            break
        if sent_this_week + sent_this_run >= args.weekly_limit:
            stop_reason = "weekly_limit"
            break

        m = SLUG_RE.search(url)
        slug = m.group(1) if m else url
        tag = f"[{i + 1}/{len(pending)}]"

        print(f"{tag} Resolving {slug} ... ", end="", flush=True)
        urn = resolve_urn(slug, headers=headers, cookie=cookie)

        if not urn:
            time.sleep(5)
            urn = resolve_urn(slug, headers=headers, cookie=cookie)

        if not urn:
            print("FAILED (could not resolve URN)")
            progress["failed"][url] = "urn-resolution-failed"
            save_progress(progress_path, progress)
            time.sleep(random.uniform(3, 8))
            continue

        print(f"{urn}")

        if args.dry_run:
            continue

        print(f"  Sending invite ... ", end="", flush=True)
        status, reason = send_connect(urn, args.message, headers=headers, cookie=cookie)

        if status == 200:
            print(f"OK")
            progress["sent"].append(url)
            sent_set.add(url)
            record_send(progress, url)
            sent_this_run += 1
        elif status == 400:
            print(f"SKIPPED ({reason})")
            progress["sent"].append(url)
            sent_set.add(url)
        elif status == 429:
            print(f"RATE LIMITED — stopping now.")
            progress["failed"][url] = f"{status} - {reason}"
            save_progress(progress_path, progress)
            stop_reason = "rate_limited"
            break
        else:
            print(f"FAILED ({status}: {reason})")
            progress["failed"][url] = f"{status} - {reason}"

        save_progress(progress_path, progress)

        delay = random.uniform(args.delay_min, args.delay_max)
        print(f"  Waiting {delay:.0f}s ...", flush=True)
        time.sleep(delay)

    if args.dry_run:
        stop_reason = "dry_run"

    weekly_total = sent_this_week + sent_this_run
    remaining_after = len(pending) - sent_this_run
    print()
    print("=" * 50)
    print(f"  Sent this run  : {sent_this_run}")
    print(f"  Sent this week : {weekly_total}/{args.weekly_limit}")
    print(f"  Total sent     : {len(progress['sent'])}")
    print(f"  Total failed   : {len(progress['failed'])}")
    print(f"  Remaining      : {max(0, remaining_after)}")
    print("=" * 50)

    if not args.dry_run:
        save_progress(progress_path, progress)
    return stop_reason


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.urls.is_file():
        print(f"ERROR: file not found: {args.urls}", file=sys.stderr)
        return 1

    progress_path = args.progress or args.urls.with_suffix(args.urls.suffix + ".progress.json")
    progress = load_progress(progress_path)

    if not args.auto:
        run_batch(args, progress_path, progress)
        return 0

    # --- auto mode: loop until every URL is processed ---
    print("AUTO MODE: will send daily batches and sleep between them until all URLs are processed.")
    print("Press Ctrl+C to stop (progress is saved; you can resume later).\n")

    while True:
        reason = run_batch(args, progress_path, progress)

        if reason == "done":
            print("\nAll URLs have been processed!")
            break
        elif reason == "dry_run":
            break
        elif reason == "rate_limited":
            sleep_secs = _seconds_until_tomorrow_8am()
            print(f"\nRate-limited. Sleeping until tomorrow morning ({_format_duration(sleep_secs)}) ...")
            time.sleep(sleep_secs)
        elif reason == "weekly_limit":
            sleep_secs = _seconds_until_next_monday_8am()
            print(f"\nWeekly limit reached. Sleeping until next Monday morning ({_format_duration(sleep_secs)}) ...")
            time.sleep(sleep_secs)
        elif reason == "daily_limit":
            sleep_secs = _seconds_until_tomorrow_8am()
            print(f"\nDaily batch done. Sleeping until tomorrow morning ({_format_duration(sleep_secs)}) ...")
            time.sleep(sleep_secs)
        else:
            break

        progress = load_progress(progress_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
