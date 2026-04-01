#!/usr/bin/env python3
"""
Calendar Sync Agent
===================
Reads events from a corporate Google Workspace calendar (via iCal URL)
and creates anonymized "Meeting" blocks in a personal Google Calendar.

Guarantees:
  - Idempotent: running N times produces the same result as running once.
  - No duplicates: deduplication via reconciliation against desired state.
  - No overlaps: overlapping/adjacent corporate events are merged into
    single contiguous blocks before syncing.
  - Only real meetings: events must have attendees. Solo events (Focus Time,
    Out of Office, personal blocks) are automatically excluded.

Designed to be run on a schedule (cron / Task Scheduler).
After initial OAuth setup for the personal calendar, runs unattended.
"""

import hashlib
import os
import sys
import logging
import argparse
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

from dotenv import load_dotenv
import requests
from icalendar import Calendar
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / "sync.log"

# Load .env file from the same directory as the script
load_dotenv(SCRIPT_DIR / ".env")

# ---------------------------------------------------------------------------
# Configuration — loaded from .env file, with sensible defaults
# ---------------------------------------------------------------------------
CONFIG = {
    # Required (no defaults — must be set in .env)
    "ical_url": os.environ.get("ICAL_URL", ""),
    "personal_calendar_id": os.environ.get("PERSONAL_CALENDAR_ID", "primary"),

    # Optional (defaults applied if not set in .env)
    "sync_days": int(os.environ.get("SYNC_DAYS", "14")),
    "block_title": os.environ.get("BLOCK_TITLE", "Meeting"),
    "block_colour": os.environ.get("BLOCK_COLOUR", "3"),  # grape = "Work" label
    "block_status": os.environ.get("BLOCK_STATUS", "opaque"),
    "dry_run": False,  # controlled via --dry-run flag, not env
}

# Paths to OAuth files (also configurable via .env)
CREDS_FILE = Path(os.environ.get("GOOGLE_CREDENTIALS_FILE", SCRIPT_DIR / "credentials.json"))
TOKEN_FILE = Path(os.environ.get("GOOGLE_TOKEN_FILE", SCRIPT_DIR / "token.json"))

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# All synced blocks carry this private extended property so we can identify them.
# The value is a fingerprint of the block's start+end time for matching.
SYNC_MARKER_KEY = "calSyncAgent"
SYNC_FINGERPRINT_KEY = "calSyncFingerprint"

# Minimum number of ATTENDEE entries for an event to count as a real meeting.
# Focus Time, Out of Office, and personal blocks have 0 attendees in the
# iCal export. Real meetings have 2+ (you + at least one other person).
MIN_ATTENDEES = 1

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
log = logging.getLogger("calendar_sync")


# ---------------------------------------------------------------------------
# Auth
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
def fingerprint(start: datetime, end: datetime) -> str:
    """
    Deterministic hash of a block's start+end time.
    Used to match desired blocks against existing synced blocks.
    """
    raw = f"{start.isoformat()}|{end.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _is_real_meeting(component) -> bool:
    """
    Returns True only if this VEVENT is a genuine meeting with other people.

    Detection logic:
      - Must not be cancelled.
      - Must have at least MIN_ATTENDEES attendee entries in the iCal data.
        Solo events (Focus Time, OOO, personal blocks) have zero attendees.
        Real meetings have 2+ (the organizer + invitees).
    """
    # Check status
    status = str(component.get("STATUS", "CONFIRMED")).upper()
    if status == "CANCELLED":
        log.debug("Skipped cancelled: %s", component.get("SUMMARY", ""))
        return False

    # Count ATTENDEE properties
    # In icalendar, if there are multiple ATTENDEE lines, component.get()
    # returns a list. If there's one, it returns a single vCalAddress.
    # If there are none, it returns None.
    attendees = component.get("ATTENDEE")
    if attendees is None:
        attendee_count = 0
    elif isinstance(attendees, list):
        attendee_count = len(attendees)
    else:
        attendee_count = 1

    if attendee_count < MIN_ATTENDEES:
        log.debug(
            "Skipped (no attendees): %s",
            component.get("SUMMARY", "(no title)"),
        )
        return False

    return True


# ---------------------------------------------------------------------------
# iCal parsing
# ---------------------------------------------------------------------------
def fetch_ical_events(ical_url: str, sync_days: int) -> list[dict]:
    """
    Fetch and parse corporate calendar events from the iCal URL.
    Returns only real meetings: timed events with attendees, not cancelled.
    Solo events (Focus Time, OOO, personal blocks) are excluded because
    they have no ATTENDEE entries in the iCal export.
    """
    log.info("Fetching iCal feed...")
    resp = requests.get(ical_url, timeout=30)
    resp.raise_for_status()
    log.info("iCal feed fetched (%d bytes)", len(resp.content))

    cal = Calendar.from_ical(resp.content)

    now = datetime.now(timezone.utc)
    window_start = now
    window_end = now + timedelta(days=sync_days)

    events = []
    skipped_types = {"all_day": 0, "no_attendees": 0, "outside_window": 0, "cancelled": 0}

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        dtstart = component.get("DTSTART")
        dtend = component.get("DTEND")
        if not dtstart or not dtend:
            continue

        start = dtstart.dt
        end = dtend.dt

        # Skip all-day events
        if isinstance(start, date) and not isinstance(start, datetime):
            skipped_types["all_day"] += 1
            continue

        # Ensure timezone-aware
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        # Skip events outside sync window
        if end <= window_start or start >= window_end:
            skipped_types["outside_window"] += 1
            continue

        # Only sync real meetings (with attendees, not cancelled)
        if not _is_real_meeting(component):
            skipped_types["no_attendees"] += 1
            continue

        # Extract timezone info
        start_tz = "UTC"
        end_tz = "UTC"
        if hasattr(dtstart, "params") and "TZID" in dtstart.params:
            start_tz = str(dtstart.params["TZID"])
        if hasattr(dtend, "params") and "TZID" in dtend.params:
            end_tz = str(dtend.params["TZID"])

        events.append({
            "start": start,
            "end": end,
            "start_tz": start_tz,
            "end_tz": end_tz,
        })

    events.sort(key=lambda e: e["start"])

    log.info(
        "Parsed iCal: %d meetings kept, %d all-day skipped, "
        "%d solo/no-attendees skipped, %d outside window.",
        len(events),
        skipped_types["all_day"],
        skipped_types["no_attendees"],
        skipped_types["outside_window"],
    )
    return events


# ---------------------------------------------------------------------------
# Overlap merging
# ---------------------------------------------------------------------------
def merge_overlapping(events: list[dict]) -> list[dict]:
    """
    Merge overlapping or adjacent events into contiguous blocks.

    Input:  sorted list of events with 'start' and 'end' datetimes.
    Output: list of merged blocks (start, end, start_tz, end_tz).

    Two events are merged if event_b.start <= event_a.end
    (i.e. they overlap or are exactly adjacent).
    """
    if not events:
        return []

    merged = []
    current = {
        "start": events[0]["start"],
        "end": events[0]["end"],
        "start_tz": events[0]["start_tz"],
        "end_tz": events[0]["end_tz"],
    }

    for ev in events[1:]:
        if ev["start"] <= current["end"]:
            # Overlapping or adjacent — extend the current block
            if ev["end"] > current["end"]:
                current["end"] = ev["end"]
                current["end_tz"] = ev["end_tz"]
        else:
            # Gap — finalize current block and start a new one
            merged.append(current)
            current = {
                "start": ev["start"],
                "end": ev["end"],
                "start_tz": ev["start_tz"],
                "end_tz": ev["end_tz"],
            }

    merged.append(current)

    if len(merged) < len(events):
        log.info(
            "Merged %d events into %d non-overlapping blocks.",
            len(events), len(merged),
        )
    return merged


# ---------------------------------------------------------------------------
# Personal calendar: fetch existing synced blocks
# ---------------------------------------------------------------------------
def fetch_existing_blocks(service, calendar_id: str, sync_days: int) -> list[dict]:
    """
    Fetch all events in the personal calendar that were created by this agent.
    Identified by the presence of the SYNC_MARKER_KEY extended property.
    Returns list of dicts with: event_id, fingerprint, start, end.
    """
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=sync_days)).isoformat()

    blocks = []
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
                blocks.append({
                    "event_id": ev["id"],
                    "fingerprint": props.get(SYNC_FINGERPRINT_KEY, ""),
                    "start": ev.get("start", {}).get("dateTime", ""),
                    "end": ev.get("end", {}).get("dateTime", ""),
                })

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    log.info("Found %d existing synced blocks in personal calendar.", len(blocks))
    return blocks


# ---------------------------------------------------------------------------
# Reconciliation: the core idempotency logic
# ---------------------------------------------------------------------------
def reconcile(
    service,
    desired_blocks: list[dict],
    existing_blocks: list[dict],
    config: dict,
) -> dict:
    """
    Compare desired state against actual state and make minimal changes.

    1. Build fingerprint sets for desired and existing blocks.
    2. DELETE existing blocks whose fingerprint is not in the desired set.
    3. CREATE desired blocks whose fingerprint is not in the existing set.
    4. LEAVE matched blocks untouched.

    Returns: {created: int, deleted: int, unchanged: int}
    """
    cal_id = config["personal_calendar_id"]
    stats = {"created": 0, "deleted": 0, "unchanged": 0}

    # Build lookup sets
    desired_fps = {}
    for block in desired_blocks:
        fp = fingerprint(block["start"], block["end"])
        desired_fps[fp] = block

    existing_fps = {}
    for block in existing_blocks:
        fp = block["fingerprint"]
        existing_fps[fp] = block

    # --- Delete stale blocks (exist in personal but not in desired) ---
    for fp, block in existing_fps.items():
        if fp not in desired_fps:
            if config["dry_run"]:
                log.info("[DRY RUN] Would delete stale block: %s → %s", block["start"], block["end"])
            else:
                try:
                    service.events().delete(
                        calendarId=cal_id, eventId=block["event_id"]
                    ).execute()
                    log.info("Deleted stale block: %s → %s", block["start"], block["end"])
                except HttpError as e:
                    log.warning("Could not delete block %s: %s", block["event_id"], e)
            stats["deleted"] += 1

    # --- Create new blocks (in desired but not yet in personal) ---
    for fp, block in desired_fps.items():
        if fp not in existing_fps:
            if config["dry_run"]:
                log.info(
                    "[DRY RUN] Would create: %s  %s → %s",
                    config["block_title"],
                    block["start"].strftime("%a %d %b %H:%M"),
                    block["end"].strftime("%H:%M"),
                )
            else:
                event_body = {
                    "summary": config["block_title"],
                    "start": {
                        "dateTime": block["start"].isoformat(),
                        "timeZone": block["start_tz"],
                    },
                    "end": {
                        "dateTime": block["end"].isoformat(),
                        "timeZone": block["end_tz"],
                    },
                    "transparency": config["block_status"],
                    "reminders": {"useDefault": False, "overrides": []},
                    "extendedProperties": {
                        "private": {
                            SYNC_MARKER_KEY: "true",
                            SYNC_FINGERPRINT_KEY: fp,
                        }
                    },
                }
                # Only set colorId if explicitly configured;
                # otherwise events inherit the target calendar's colour.
                if config["block_colour"]:
                    event_body["colorId"] = config["block_colour"]

                service.events().insert(calendarId=cal_id, body=event_body).execute()
                log.info(
                    "Created: %s  %s → %s",
                    config["block_title"],
                    block["start"].strftime("%a %d %b %H:%M"),
                    block["end"].strftime("%H:%M"),
                )
            stats["created"] += 1
        else:
            stats["unchanged"] += 1

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Sync corporate calendar (iCal) → personal Google Calendar as anonymized blocks"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log what would happen without making changes"
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Override sync window (default: 14 days)"
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

    # --- Validate ---
    if not CONFIG["ical_url"]:
        log.error(
            "ICAL_URL is not set.\n"
            "Add it to your .env file (see .env.example) or set it as\n"
            "an environment variable before running the script."
        )
        sys.exit(1)

    # --- Step 1: Fetch corporate events via iCal ---
    try:
        raw_events = fetch_ical_events(CONFIG["ical_url"], CONFIG["sync_days"])
    except requests.RequestException as e:
        log.error("Failed to fetch iCal feed: %s", e)
        sys.exit(1)

    # --- Step 2: Merge overlapping events into contiguous blocks ---
    desired_blocks = merge_overlapping(raw_events)
    log.info("Desired state: %d blocks for the next %d days.", len(desired_blocks), CONFIG["sync_days"])

    # --- Step 3: Authenticate with personal calendar ---
    log.info("Authenticating with personal Google Calendar...")
    service = get_personal_service()

    # --- Step 4: Fetch existing synced blocks ---
    existing_blocks = fetch_existing_blocks(
        service, CONFIG["personal_calendar_id"], CONFIG["sync_days"]
    )

    # --- Step 5: Reconcile (the idempotent core) ---
    try:
        stats = reconcile(service, desired_blocks, existing_blocks, CONFIG)
        log.info(
            "Sync complete — created: %d, deleted: %d, unchanged: %d",
            stats["created"],
            stats["deleted"],
            stats["unchanged"],
        )
    except HttpError as e:
        log.error("Google Calendar API error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
