# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-file Python tool that syncs a corporate Google Workspace calendar (via iCal URL) to a personal Google Calendar as anonymized "Meeting" blocks. Designed to run unattended on a schedule (cron) after initial OAuth setup.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run sync
python sync_calendar.py

# Dry run (log what would happen, no changes)
python sync_calendar.py --dry-run

# Override sync window
python sync_calendar.py --days 7

# Verbose/debug logging
python sync_calendar.py -v
```

## Setup

1. Copy `.env.example` to `.env` and set `ICAL_URL` (required)
2. Place Google OAuth `credentials.json` in the project root (from Google Cloud Console)
3. First run opens a browser for OAuth consent; subsequent runs use saved `token.json`

## Architecture

Everything lives in `sync_calendar.py`. The sync pipeline is a 5-step process in `main()`:

1. **Fetch** corporate events via iCal URL (`fetch_ical_events`) — filters to real meetings only (must have attendees, not cancelled, not all-day)
2. **Merge** overlapping/adjacent events into contiguous blocks (`merge_overlapping`)
3. **Authenticate** with personal Google Calendar via OAuth (`get_personal_service`)
4. **Fetch existing** synced blocks from personal calendar (`fetch_existing_blocks`) — identified by `calSyncAgent` extended property
5. **Reconcile** desired vs actual state (`reconcile`) — fingerprint-based diffing ensures idempotency: only creates missing blocks and deletes stale ones

Key design decisions:
- **Idempotency via fingerprinting**: Each block gets a SHA-256 fingerprint of its start+end time. Reconciliation compares fingerprint sets rather than doing event-by-event matching.
- **Extended properties as sync markers**: All synced events carry `calSyncAgent=true` and `calSyncFingerprint=<hash>` private extended properties, so the tool only touches its own events.
- **Meeting detection**: Events must have `MIN_ATTENDEES` (1) attendee entries. Solo events (Focus Time, OOO) have 0 attendees in iCal exports and are excluded.
- **All config via `.env`**: `ICAL_URL`, `PERSONAL_CALENDAR_ID`, `SYNC_DAYS`, `BLOCK_TITLE`, `BLOCK_COLOUR`, `BLOCK_STATUS`. See `.env.example` for full list.

## MCP Server — Google Calendar

A Google Calendar MCP server is available for direct calendar access from Claude Code. Connect it with `/mcp` and authenticate when prompted.

Available tools (prefixed `mcp__claude_ai_Google_Calendar__`):
- `gcal_list_calendars` — list all calendars on the account
- `gcal_list_events` — list events in a calendar (supports time range filtering)
- `gcal_get_event` / `gcal_create_event` / `gcal_update_event` / `gcal_delete_event` — CRUD on individual events
- `gcal_find_free_time` / `gcal_find_meeting_times` — availability queries
- `gcal_respond_to_event` — accept/decline/tentative

Useful for debugging sync issues: list events on the personal calendar to verify synced blocks, check extended properties, or inspect the corporate calendar directly.

## Sensitive Files

Never commit: `.env`, `credentials.json`, `token.json` (all in `.gitignore`).
