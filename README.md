# LinkedIn Connect Automation

Automate LinkedIn connection requests via the Voyager API. Reads a text file of profile URLs, resolves each to an internal URN, and sends connection invitations with a custom note.

## Requirements

- Python 3.9+ (no external dependencies -- stdlib only)
- A LinkedIn session: `li_at` cookie and `JSESSIONID` (CSRF token)

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

