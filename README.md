# LinkedIn Connect Automation

Automate LinkedIn connection requests via the Voyager API. Reads a text file of profile URLs, resolves each to an internal URN, and sends connection invitations with a custom note.

## Requirements

- Python 3.9+ (no external dependencies -- stdlib only)
- A LinkedIn session: `li_at` cookie and `JSESSIONID` (CSRF token)
- **LinkedIn Premium** is required to send unlimited connection notes. Free accounts are limited to ~5 personalized notes per month; without Premium the `--message` text will be silently dropped on most invites.

## Getting Your Session Cookies

1. Open LinkedIn in Chrome
2. Open DevTools (`Cmd+Option+I`) -> **Application** tab -> **Cookies** -> `https://www.linkedin.com`
3. Copy the value of `**li_at`** (long base64 string)
4. Copy the value of `**JSESSIONID**` -- strip the surrounding double-quotes, e.g. `"ajax:1067..."` becomes `ajax:1067...`

## Usage

### Basic run (sends up to 25, then stops)

```bash
python3 linkedin_connect.py \
  --urls linkedins.txt \
  --cookie "YOUR_LI_AT" \
  --csrf "ajax:YOUR_JSESSIONID" \
  --message 'Connecting from XXX...'
```

### Dry run (resolve URNs only, send nothing)

```bash
python3 linkedin_connect.py \
  --urls linkedins.txt \
  --cookie "YOUR_LI_AT" \
  --csrf "ajax:YOUR_JSESSIONID" \
  --dry-run
```

### Auto mode (fire-and-forget, runs for days)

Sends one daily batch, sleeps until ~8 AM next morning, repeats. Stops at the weekly limit and sleeps until Monday, then resumes.

```bash
python3 linkedin_connect.py \
  --urls linkedins.txt \
  --cookie "YOUR_LI_AT" \
  --csrf "ajax:YOUR_JSESSIONID" \
  --message 'Connecting from XXX...' \
  --auto
```

## CLI Arguments


| Argument         | Required | Default                  | Description                                      |
| ---------------- | -------- | ------------------------ | ------------------------------------------------ |
| `--urls`         | Yes      | --                       | Text file with one LinkedIn profile URL per line |
| `--cookie`       | Yes      | --                       | `li_at` session cookie value                     |
| `--csrf`         | Yes      | --                       | `JSESSIONID` value (strip outer quotes)          |
| `--message`      | No       | `Connecting from XXX...` | Note attached to each connection request         |
| `--daily-limit`  | No       | `25`                     | Max invites per daily batch                      |
| `--weekly-limit` | No       | `150`                    | Max invites per Mon-Sun week                     |
| `--delay-min`    | No       | `45`                     | Min seconds between requests                     |
| `--delay-max`    | No       | `120`                    | Max seconds between requests                     |
| `--progress`     | No       | `<urls>.progress.json`   | Path to progress tracking file                   |
| `--dry-run`      | No       | `false`                  | Resolve URNs and print results without sending   |
| `--auto`         | No       | `false`                  | Run continuously across days/weeks until done    |


## Input Format

Plain text, one LinkedIn URL per line. Blank lines and duplicates are ignored.

```
https://www.linkedin.com/in/xxxx
https://www.linkedin.com/in/xxxx-rocha
https://www.linkedin.com/in/xxxx
```

## Progress Tracking

A JSON file (default: `<urls_file>.progress.json`) tracks:

- **sent** -- URLs that got a 200 (invite sent) or 400 (already connected)
- **failed** -- URLs that errored, with the reason
- **send_log** -- timestamped record of actual invites, used for weekly limit enforcement

Re-running automatically skips already-processed URLs. Delete the progress file to start fresh.

## Behavior Notes

- **400 responses** are treated as "already connected or pending invite" and skipped (not counted toward limits)
- **429 responses** trigger an immediate stop; in `--auto` mode, it sleeps until the next morning
- **Ctrl+C** is safe at any time -- progress is saved after every request
- **Weekly limit** resets each Monday at midnight UTC
- **Auto mode** sleeps until ~8:00 AM local time (with random jitter up to 30 min)

---

## Bulk LinkedIn Profile Finder (`linkedin_finder.py`)

Companion script that bulk-finds LinkedIn profile URLs for employees of **any company** using Google search via [SerpAPI](https://serpapi.com/). You specify the company name (and optional aliases) on the command line; the script searches for each person, filters results to those mentioning your company, and fills column F (`Linkedin Links`) of the input CSV in place. Progress is checkpointed so large jobs are safely resumable.

### Setup

```bash
export SERPAPI_KEY="your-serpapi-key-here"
```

Input CSV must have at least 6 columns. The script uses columns A–B as **First Name** and **Last Name**, and writes matched LinkedIn URLs into column F.

### Usage

Dry-run on a small sample (no CSV writes; results cached in progress file):

```bash
python3 linkedin_finder.py \
  --csv /path/to/employees.csv \
  --company "Acme Corp" \
  --limit 25 --dry-run
```

Full run with company aliases (abbreviations, former names, etc.):

```bash
python3 linkedin_finder.py \
  --csv /path/to/employees.csv \
  --company CLA \
  --company-aliases CliftonLarsonAllen "Clifton Larson Allen"
```

Tuned run (more workers, larger flush interval):

```bash
python3 linkedin_finder.py \
  --csv /path/to/employees.csv \
  --company Google \
  --company-aliases Alphabet GOOG \
  --workers 8 --batch-size 100
```

### CLI Arguments

| Argument            | Required | Default        | Description                                                |
| ------------------- | -------- | -------------- | ---------------------------------------------------------- |
| `--csv`             | Yes      | --             | Input CSV (column F is filled in place)                    |
| `--company`         | Yes      | --             | Primary company name to match in search results            |
| `--company-aliases` | No       | --             | Additional names/abbreviations for the company             |
| `--api-key`         | No       | `$SERPAPI_KEY`  | SerpAPI key                                                |
| `--start`           | No       | `0`            | Start at this row index (0-based, excludes header)         |
| `--limit`           | No       | --             | Process at most N pending rows                             |
| `--batch-size`      | No       | `50`           | Flush CSV + progress every N completions                   |
| `--workers`         | No       | `5`            | Parallel SerpAPI workers                                   |
| `--dry-run`         | No       | `false`        | Run searches without writing CSV (progress still cached)   |
| `--retry-no-match`  | No       | `false`        | Re-process rows previously marked completed with no match  |
| `--retry-errors`    | No       | `false`        | Re-process rows that previously errored (default: skip)    |
| `--force`           | No       | `false`        | Bypass all caches and re-process every row from scratch    |
| `--max-per-minute`  | No       | `45`           | Global cap on SerpAPI requests per rolling 60s window      |

### How Matching Works

For each row, the script issues this Google query through SerpAPI:

```text
site:linkedin.com/in "FirstName LastName" (CompanyName OR "Alias One" OR AliasTwo)
```

The parenthesized OR clause is built automatically from `--company` and `--company-aliases`.

For each `organic_result`:

1. Keep only `linkedin.com/in/<slug>` URLs.
2. Require the title/snippet text to match the company name or any alias (case-insensitive word-boundary regex).
3. Dedupe by lowercase slug.
4. Take up to 5 distinct matches and join them with ` | ` into column F.

If zero results match the strict query, one fallback query **without** the company filter is issued and the same snippet regex is reapplied. This catches cases where Google truncates the company name in the snippet.

### Sidecar Files (written next to the CSV)

All sidecar files are derived from the CSV filename (e.g. for `employees.csv`):

- `employees.progress.json` — per-row checkpoint: `{ "completed": {"name": [urls]}, "errors": {"name": msg} }`. Delete to start fresh.
- `employees_no_match.txt` — rows where no company-affiliated profile was found.
- `employees_ambiguous.txt` — rows where multiple plausible URLs were written.

### Behavior Notes

- **Idempotent reruns**: running the script a second time on a completed CSV is a true no-op — zero SerpAPI calls, zero CSV writes (other than a deterministic refresh of sidecars). Three caches gate re-processing:
  1. Column F already populated in the CSV.
  2. Row's name appears in `progress["completed"]` (matched **or** explicitly no-match).
  3. Row's name appears in `progress["errors"]` (skipped by default — set `--retry-errors` to retry).
- **Name-keyed progress**: `progress.json` is keyed by normalized `firstname lastname` (lowercased, whitespace-collapsed) rather than CSV row index, so reordering or inserting rows does not corrupt the cache. A v1→v2 migration runs automatically on first execution against an older index-keyed progress file.
- **Deterministic sidecars**: the no-match and ambiguous files are **rewritten** (not appended) from progress state on every flush, so reruns produce byte-identical outputs.
- **Force re-run**: pass `--force` to clear column F + progress in memory and reprocess every row from scratch.
- **Batched + atomic writes**: every `--batch-size` completions, the CSV is rewritten via `tempfile` + `os.replace`, and the progress JSON is fsynced. Crash-safe.
- **Concurrency**: `ThreadPoolExecutor` issues SerpAPI calls in parallel; result aggregation and writes are single-threaded.
- **Rate limiting**: a global sliding-window limiter caps requests to `--max-per-minute` (default 45 ≈ 2,700/hr, safely below SerpAPI's 3,000/hr production cap). On HTTP 429, each request sleeps 90s and retries up to 30 times (~45 min worst case) so the run survives temporary throttling.
- **Backoff**: HTTP 5xx and network errors retry with exponential backoff (2s/4s/8s/16s/32s, up to 5 attempts) before marking the row as `error`.
- **Ctrl+C** is safe: in-flight tasks finish, then CSV + progress are flushed before exit. A second Ctrl+C exits immediately.
- **Cost**: ~1 SerpAPI credit per row, plus ~1 extra credit for each row that needed the fallback query.

