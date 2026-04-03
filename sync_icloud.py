#!/usr/bin/env python3
"""
iCloud Calendar Sync
====================
Copies events from a local iCloud calendar (via macOS EventKit) into a
personal Google Calendar, preserving original titles and details.

Uses the Mac's existing iCloud connection — no separate authentication
or public calendar sharing required.

Guarantees:
  - Idempotent: running N times produces the same result as running once.
  - No duplicates: fingerprint-based reconciliation.
  - Copies all events (timed and all-day) within the sync window.

Designed to be run on a schedule (cron / launchd).
After initial OAuth setup for the personal calendar, runs unattended.

Requires: pyobjc-framework-EventKit (pip install pyobjc-framework-EventKit)
"""

import fcntl
import hashlib
import os
import sys
import logging
import argparse
import threading
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import EventKit
from Foundation import NSDate
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / "sync_icloud.log"
LOCK_FILE = SCRIPT_DIR / ".sync_icloud.lock"

# Load .env file from the same directory as the script
load_dotenv(SCRIPT_DIR / ".env")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG = {
    "icloud_calendar_name": os.environ.get("ICLOUD_CALENDAR_NAME", ""),
    "personal_calendar_id": os.environ.get("ICLOUD_TARGET_CALENDAR_ID",
                                           os.environ.get("PERSONAL_CALENDAR_ID", "primary")),
    "sync_days": int(os.environ.get("ICLOUD_SYNC_DAYS", os.environ.get("SYNC_DAYS", "14"))),
    "block_colour": os.environ.get("ICLOUD_BLOCK_COLOUR", ""),
    "block_status": os.environ.get("ICLOUD_BLOCK_STATUS", "opaque"),
    "dry_run": False,
}

# OAuth files — shared with the corporate sync
CREDS_FILE = Path(os.environ.get("GOOGLE_CREDENTIALS_FILE", SCRIPT_DIR / "credentials.json"))
TOKEN_FILE = Path(os.environ.get("GOOGLE_TOKEN_FILE", SCRIPT_DIR / "token.json"))

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Sync markers — distinct from the corporate sync so they don't interfere
SYNC_MARKER_KEY = "calSyncIcloud"
SYNC_FINGERPRINT_KEY = "calSyncIcloudFingerprint"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("icloud_sync")


# ---------------------------------------------------------------------------
# Google Calendar Auth (same as corporate sync — shares token)
# ---------------------------------------------------------------------------
def get_personal_service():
    """Authenticate with the personal Google Calendar via OAuth."""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing expired token...")
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                log.error(
                    "OAuth credentials file not found: %s\n"
                    "Download it from Google Cloud Console. See README.md.",
                    CREDS_FILE,
                )
                sys.exit(1)
            log.info("Starting OAuth flow — a browser window will open.")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())
        log.info("Token saved to %s", TOKEN_FILE.name)

    return build("calendar", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fingerprint(summary: str, start_iso: str, end_iso: str, all_day: bool) -> str:
    """Deterministic hash of an event's identity (title + time range)."""
    raw = f"{summary}|{start_iso}|{end_iso}|{all_day}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _nsdate_to_datetime(nsdate) -> datetime:
    """Convert an NSDate to a timezone-aware Python datetime (UTC)."""
    # NSDate timeIntervalSince1970 gives Unix timestamp
    ts = nsdate.timeIntervalSince1970()
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# ---------------------------------------------------------------------------
# EventKit: read iCloud calendar events
# ---------------------------------------------------------------------------
def get_eventkit_store():
    """
    Create an EKEventStore and request calendar access.
    Uses the Mac's existing iCloud connection.
    """
    store = EventKit.EKEventStore.alloc().init()

    # Check current authorization
    status = EventKit.EKEventStore.authorizationStatusForEntityType_(
        EventKit.EKEntityTypeEvent
    )

    if status == 4:  # EKAuthorizationStatusFullAccess
        log.debug("Calendar access already granted.")
        return store

    if status == 2:  # EKAuthorizationStatusDenied
        log.error(
            "Calendar access denied. Grant access in:\n"
            "  System Settings → Privacy & Security → Calendars → enable for Python/Terminal"
        )
        sys.exit(1)

    # Request access (triggers macOS permission dialog on first run)
    result = threading.Event()
    access_granted = [False]
    access_error = [None]

    def callback(granted, error):
        access_granted[0] = granted
        access_error[0] = error
        result.set()

    store.requestFullAccessToEventsWithCompletion_(callback)
    result.wait(timeout=30)

    if not access_granted[0]:
        err_msg = access_error[0] if access_error[0] else "unknown"
        log.error("Calendar access not granted: %s", err_msg)
        sys.exit(1)

    return store


def find_icloud_calendar(store, calendar_name: str):
    """Find an iCloud calendar by name."""
    calendars = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)

    # Filter to iCloud source (sourceType 2 = CalDAV, which includes iCloud)
    icloud_cals = []
    for cal in calendars:
        src = cal.source()
        if src and src.title() == "iCloud":
            icloud_cals.append(cal)

    # Match by name
    for cal in icloud_cals:
        if cal.title() == calendar_name:
            return cal

    # If no exact match, try case-insensitive
    for cal in icloud_cals:
        if cal.title().lower() == calendar_name.lower():
            log.warning("Case-insensitive match: '%s' → '%s'", calendar_name, cal.title())
            return cal

    # Not found — show available calendars
    available = [cal.title() for cal in icloud_cals]
    log.error(
        "iCloud calendar '%s' not found.\nAvailable iCloud calendars: %s",
        calendar_name,
        ", ".join(f'"{n}"' for n in available),
    )
    sys.exit(1)


def fetch_eventkit_events(store, calendar, sync_days: int) -> list[dict]:
    """
    Fetch events from a macOS EventKit calendar.
    Returns all events (timed and all-day) within the sync window,
    preserving original titles, descriptions, and locations.
    """
    now = datetime.now(timezone.utc)
    start_date = NSDate.date()  # now
    end_date = NSDate.dateWithTimeIntervalSinceNow_(sync_days * 86400)

    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        start_date, end_date, [calendar]
    )
    ek_events = store.eventsMatchingPredicate_(predicate)

    log.info("EventKit returned %d events from '%s'.", len(ek_events), calendar.title())

    events = []
    for ek_ev in ek_events:
        # Skip cancelled events
        if ek_ev.status() == 3:  # EKEventStatusCanceled
            continue

        summary = ek_ev.title() or "(no title)"

        # Skip travel/location marker events
        if summary.startswith("Simon in"):
            log.debug("Skipped (title filter): %s", summary)
            continue

        description = ek_ev.notes() or ""
        location = ek_ev.location() or ""
        all_day = ek_ev.isAllDay()

        start_dt = _nsdate_to_datetime(ek_ev.startDate())
        end_dt = _nsdate_to_datetime(ek_ev.endDate())

        # Determine timezone from the event's timeZone or the calendar's timeZone
        ek_tz = ek_ev.timeZone()
        if ek_tz:
            tz_name = ek_tz.name()
        else:
            cal_tz = calendar.timeZone() if hasattr(calendar, 'timeZone') else None
            tz_name = cal_tz.name() if cal_tz else "UTC"

        if all_day:
            # For all-day events, store as date objects
            events.append({
                "summary": summary,
                "description": description,
                "location": location,
                "start": start_dt.date(),
                "end": end_dt.date(),
                "all_day": True,
                "start_tz": None,
                "end_tz": None,
            })
        else:
            events.append({
                "summary": summary,
                "description": description,
                "location": location,
                "start": start_dt,
                "end": end_dt,
                "all_day": False,
                "start_tz": tz_name,
                "end_tz": tz_name,
            })

    events.sort(key=lambda e: (
        e["start"] if isinstance(e["start"], date) and not isinstance(e["start"], datetime)
        else e["start"].date() if isinstance(e["start"], datetime) else e["start"],
        0 if e["all_day"] else 1,
    ))

    log.info("Parsed %d events (%d timed, %d all-day).",
             len(events),
             sum(1 for e in events if not e["all_day"]),
             sum(1 for e in events if e["all_day"]))
    return events


# ---------------------------------------------------------------------------
# Personal calendar: fetch existing synced events
# ---------------------------------------------------------------------------
def fetch_existing_events(service, calendar_id: str, sync_days: int) -> list[dict]:
    """
    Fetch all events in the personal calendar that were created by this agent.
    Identified by the SYNC_MARKER_KEY extended property.
    """
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=sync_days)).isoformat()

    events = []
    page_token = None

    while True:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        for ev in result.get("items", []):
            props = ev.get("extendedProperties", {}).get("private", {})
            if props.get(SYNC_MARKER_KEY) == "true":
                events.append({
                    "event_id": ev["id"],
                    "fingerprint": props.get(SYNC_FINGERPRINT_KEY, ""),
                })

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    log.info("Found %d existing iCloud-synced events in personal calendar.", len(events))
    return events


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def reconcile(service, desired_events: list[dict], existing_events: list[dict], config: dict) -> dict:
    """
    Compare desired state against actual state and make minimal changes.
    Fingerprint-based: only creates missing events and deletes stale ones.
    """
    cal_id = config["personal_calendar_id"]
    stats = {"created": 0, "deleted": 0, "unchanged": 0}

    # Build fingerprint sets
    desired_fps = {}
    for ev in desired_events:
        start_iso = ev["start"].isoformat()
        end_iso = ev["end"].isoformat()
        fp = fingerprint(ev["summary"], start_iso, end_iso, ev["all_day"])
        desired_fps[fp] = ev

    existing_fps = {}
    for ev in existing_events:
        existing_fps[ev["fingerprint"]] = ev

    # Delete stale events
    for fp, ev in existing_fps.items():
        if fp not in desired_fps:
            if config["dry_run"]:
                log.info("[DRY RUN] Would delete stale event: %s", ev["event_id"])
            else:
                try:
                    service.events().delete(
                        calendarId=cal_id, eventId=ev["event_id"]
                    ).execute()
                    log.info("Deleted stale event: %s", ev["event_id"])
                except HttpError as e:
                    log.warning("Could not delete event %s: %s", ev["event_id"], e)
            stats["deleted"] += 1

    # Create new events
    for fp, ev in desired_fps.items():
        if fp not in existing_fps:
            if config["dry_run"]:
                log.info("[DRY RUN] Would create: %s (%s)", ev["summary"], ev["start"])
            else:
                event_body = {
                    "summary": ev["summary"],
                    "transparency": config["block_status"],
                    "reminders": {"useDefault": False, "overrides": []},
                    "extendedProperties": {
                        "private": {
                            SYNC_MARKER_KEY: "true",
                            SYNC_FINGERPRINT_KEY: fp,
                        }
                    },
                }

                if ev["description"]:
                    event_body["description"] = ev["description"]
                if ev["location"]:
                    event_body["location"] = ev["location"]

                if config["block_colour"]:
                    event_body["colorId"] = config["block_colour"]

                if ev["all_day"]:
                    event_body["start"] = {"date": ev["start"].isoformat()}
                    event_body["end"] = {"date": ev["end"].isoformat()}
                else:
                    event_body["start"] = {
                        "dateTime": ev["start"].isoformat(),
                        "timeZone": ev["start_tz"],
                    }
                    event_body["end"] = {
                        "dateTime": ev["end"].isoformat(),
                        "timeZone": ev["end_tz"],
                    }

                service.events().insert(calendarId=cal_id, body=event_body).execute()
                log.info("Created: %s (%s)", ev["summary"], ev["start"])
            stats["created"] += 1
        else:
            stats["unchanged"] += 1

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Sync iCloud calendar → personal Google Calendar (copies events with original titles)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log what would happen without making changes"
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Override sync window (default from ICLOUD_SYNC_DAYS or SYNC_DAYS)"
    )
    parser.add_argument(
        "--calendar", type=str, default=None,
        help="Override iCloud calendar name (default from ICLOUD_CALENDAR_NAME)"
    )
    parser.add_argument(
        "--list-calendars", action="store_true",
        help="List available iCloud calendars and exit"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    if args.dry_run:
        CONFIG["dry_run"] = True
        log.info("=== DRY RUN MODE ===")
    if args.days:
        CONFIG["sync_days"] = args.days
    if args.calendar:
        CONFIG["icloud_calendar_name"] = args.calendar

    # Step 1: Connect to EventKit
    log.info("Connecting to macOS EventKit...")
    store = get_eventkit_store()

    # List calendars mode
    if args.list_calendars:
        calendars = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
        print("\nAvailable calendars:")
        for cal in calendars:
            src = cal.source()
            source_name = src.title() if src else "Unknown"
            print(f"  {cal.title()}  (source: {source_name})")
        return

    # Validate calendar name
    cal_name = CONFIG["icloud_calendar_name"]
    if not cal_name:
        log.error(
            "ICLOUD_CALENDAR_NAME is not set.\n"
            "Add it to your .env file, or use --calendar <name>.\n"
            "Run with --list-calendars to see available calendars."
        )
        sys.exit(1)

    # Step 2: Find the iCloud calendar
    calendar = find_icloud_calendar(store, cal_name)
    log.info("Syncing iCloud calendar: '%s'", calendar.title())

    # Step 3: Fetch iCloud events via EventKit
    desired_events = fetch_eventkit_events(store, calendar, CONFIG["sync_days"])
    log.info("Desired state: %d events for the next %d days.", len(desired_events), CONFIG["sync_days"])

    # Step 4: Authenticate with personal Google Calendar
    log.info("Authenticating with personal Google Calendar...")
    service = get_personal_service()

    # Step 5: Fetch existing synced events
    existing_events = fetch_existing_events(
        service, CONFIG["personal_calendar_id"], CONFIG["sync_days"]
    )

    # Step 6: Reconcile
    try:
        stats = reconcile(service, desired_events, existing_events, CONFIG)
        log.info(
            "Sync complete — created: %d, deleted: %d, unchanged: %d",
            stats["created"], stats["deleted"], stats["unchanged"],
        )
    except HttpError as e:
        log.error("Google Calendar API error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("Another instance is already running — exiting.")
        sys.exit(0)
    try:
        main()
    finally:
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()
