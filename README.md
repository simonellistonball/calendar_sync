# Calendar Sync

Syncs a corporate Google Workspace calendar to a personal Google Calendar as anonymized "Meeting" blocks. Overlapping meetings are merged into single contiguous blocks. Runs idempotently — safe to schedule on a cron.

## What it does

- Reads your corporate calendar via its secret iCal URL (read-only, no OAuth needed for the source)
- Creates anonymized time blocks on your personal Google Calendar
- Merges overlapping/adjacent meetings into single blocks
- Skips solo events (Focus Time, Out of Office, personal blocks) — only real meetings with attendees are synced
- On repeat runs, only adds new blocks and removes stale ones (fingerprint-based reconciliation)

## Prerequisites

- Python 3.10+
- A Google Cloud project with the Calendar API enabled
- OAuth 2.0 credentials for your personal Google account

## Installation

```bash
git clone <repo-url>
cd calendar_sync
pip install -r requirements.txt
```

## Configuration

### 1. Get your corporate calendar iCal URL

1. Open [Google Calendar](https://calendar.google.com)
2. Click the gear icon > **Settings**
3. In the left sidebar, click your **corporate calendar** (under your work account)
4. Scroll to **Integrate calendar**
5. Copy the **Secret address in iCal format**

> Treat this URL like a password — anyone with it can read your calendar.

### 2. Create Google OAuth credentials (for your personal calendar)

The script needs OAuth credentials to write to your personal Google Calendar.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Enable the **Google Calendar API**:
   - Navigate to **APIs & Services > Library**
   - Search for "Google Calendar API" and click **Enable**
4. Create OAuth credentials:
   - Go to **APIs & Services > Credentials**
   - Click **Create Credentials > OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON file and save it as `credentials.json` in the project root
5. Configure the OAuth consent screen:
   - Go to **APIs & Services > OAuth consent screen**
   - User type: **External** (or Internal if using a Workspace account)
   - Add yourself as a test user if the app is in "Testing" status

### 3. Set up your `.env` file

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```
ICAL_URL=https://calendar.google.com/calendar/ical/...../basic.ics
```

Optional settings (defaults shown):

| Variable | Default | Description |
|---|---|---|
| `PERSONAL_CALENDAR_ID` | `primary` | Target calendar ID (find in Google Calendar Settings > Integrate calendar > Calendar ID) |
| `SYNC_DAYS` | `14` | How many days ahead to sync |
| `BLOCK_TITLE` | `Meeting` | Title shown on synced blocks |
| `BLOCK_COLOUR` | `3` (grape) | Google Calendar colour ID (1-11, see `.env.example` for the full map) |
| `BLOCK_STATUS` | `opaque` | `opaque` = shows as busy, `transparent` = shows as free |
| `GOOGLE_CREDENTIALS_FILE` | `./credentials.json` | Path to OAuth client secrets |
| `GOOGLE_TOKEN_FILE` | `./token.json` | Path to saved OAuth token |

### 4. Authorize on first run

```bash
python sync_calendar.py
```

A browser window will open for Google OAuth consent. Sign in with your **personal** Google account and grant calendar access. The token is saved to `token.json` for future runs.

## Usage

```bash
# Standard sync
python sync_calendar.py

# Preview changes without modifying anything
python sync_calendar.py --dry-run

# Sync a different number of days
python sync_calendar.py --days 7

# Debug logging
python sync_calendar.py -v

# Combine flags
python sync_calendar.py --dry-run --days 30 -v
```

## Running on a schedule

### macOS (launchd)

```bash
# Install with default 15-minute interval
./install-launchd.sh

# Or set a custom interval (in seconds)
./install-launchd.sh 600   # every 10 minutes
```

The script auto-detects paths, installs a launchd plist, loads it, and runs an initial sync immediately. To manage:

```bash
launchctl list com.simonellistonball.calendar-sync                              # check status
launchctl unload ~/Library/LaunchAgents/com.simonellistonball.calendar-sync.plist  # stop
launchctl load ~/Library/LaunchAgents/com.simonellistonball.calendar-sync.plist    # start
```

### Linux (cron)

```bash
crontab -e
```

```
*/15 * * * * cd /path/to/calendar_sync && /usr/bin/python3 sync_calendar.py >> /dev/null 2>&1
```

## Logs

All runs are logged to `sync.log` in the project directory and to stdout.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ICAL_URL is not set` | Add `ICAL_URL=...` to your `.env` file |
| `OAuth credentials file not found` | Download `credentials.json` from Google Cloud Console (see step 2 above) |
| `Token has been expired or revoked` | Delete `token.json` and re-run to re-authorize |
| `HttpError 403: insufficient permissions` | Ensure the Calendar API is enabled and you granted calendar access during OAuth |
| Blocks not appearing | Check `--dry-run` output; verify `PERSONAL_CALENDAR_ID` points to the right calendar |
| Solo events being synced | They shouldn't be — only events with attendees are synced. File a bug if you see this |
