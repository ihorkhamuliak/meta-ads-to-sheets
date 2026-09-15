# Meta Ads → Google Sheets Automation

Automated daily sync of Facebook/Instagram ad campaign spend to a client's Google Sheet — no manual exports, no copy-paste.

## What it does

Every morning (GitHub Actions cron `0 6 * * *`, 06:00 UTC; the actual start can lag) this script:
1. Pulls campaign-level insights from the Meta Marketing API for the last 3 days
2. Aggregates total daily spend across all campaigns
3. Writes the value to the correct row in the client's existing tracker sheet, in one of two modes (below)

The client opens their Google Sheet in the morning and the numbers are already there.

## Tech stack

| Layer | Tool |
|---|---|
| Ad data | Meta Marketing API (Graph API v21.0) |
| Sheets | Google Sheets API via `gspread` + service account |
| Scheduler | GitHub Actions cron (`0 6 * * *`) |
| Language | Python 3.11 |

## Project structure

```
meta-ads-to-sheets/
├── src/
│   ├── config.py        # env-var config, typed dataclass
│   ├── meta_client.py   # Meta API calls, retry + backoff
│   ├── sheets_client.py # Google Sheets read/write
│   └── main.py          # entry point
├── .github/
│   └── workflows/
│       └── daily.yml    # GitHub Actions cron job
├── .env.example
└── requirements.txt
```

## Key implementation details

- **Retry logic** — exponential backoff on Meta API rate-limit (codes 613, 80004) and transient HTTP errors (429, 5xx)
- **Idempotent writes** — lookback window of 3 days handles Meta's attribution updates; re-running never duplicates data
- **Dynamic tab resolution** (`monthly_tabs`) — tab name built from date (`June (2026)`, `July (2026)`, …) so the script works across months automatically
- **Date lookup** — finds the correct row by matching `DD.MM.YYYY` date string in column B; no hardcoded row numbers
- **Credentials** — service account JSON accepted as a file path (local) or raw JSON string (CI secret), same code path

## Sheet modes (`CLIENT_MODE`)

| Mode | Sheet layout | What gets written |
|---|---|---|
| `monthly_tabs` (default) | one tab per month, e.g. `June (2026)` | daily spend in the ad account currency |
| `tracker_usd` | one tab (`CLIENT_TAB_NAME`), day rows under a `День \| Дата` header, dates as sheet serials | daily spend converted to USD at the official NBP rate for that day (last published rate on weekends); only the spend column, formula columns are never touched |

`tracker_usd` only updates rows that already exist, never appends, and checks the account currency is PLN before converting.
After writing it reads the cells back and compares the neighbouring formulas before and after.

**Failures are loud, not silent.** Missing tab, header, date row or exchange rate stops the run with a non-zero exit code (4 = sheet write, 5 = exchange rate), so GitHub emails the owner instead of skipping a day.

**Manual runs** (`workflow_dispatch`) accept `sheet_id`, `dry_run` and `lookback_days`, so a new sheet can be tested without touching secrets.

## Local setup

```bash
git clone https://github.com/ihorkhamuliak/meta-ads-to-sheets
cd meta-ads-to-sheets
pip install -r requirements.txt
cp .env.example .env
# fill in .env, then:
python -m src.main
```

## GitHub Actions setup

1. Fork / clone the repo
2. Add four repository secrets:

| Secret | Value |
|---|---|
| `META_ACCESS_TOKEN` | System User token with `ads_read` + `read_insights` |
| `META_AD_ACCOUNT_ID` | `act_XXXXXXXXX` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON content of the service account key |
| `CLIENT_SHEET_ID` | Google Sheet ID from the URL |

3. Run manually from the **Actions** tab to verify, then the cron takes over.

## Result

- **Before:** account manager exports CSV from Meta Ads Manager, pastes into sheet manually — ~15 min/day
- **After:** data appears automatically every morning — 0 min/day
- First automated run: wrote spend data for 3 days (Jun 8–10, 2026) in under 30 seconds
