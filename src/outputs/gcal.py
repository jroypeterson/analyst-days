"""Google Calendar output.

Writes confirmed events to the same calendar earnings_agent uses (set via
GOOGLE_CALENDAR_ID). Each analyst-days event is an ALL-DAY block with a
type-prefixed title so it's distinguishable from earnings entries on the
same calendar:

  Investor Day: AFRM
  Analyst Day: TICKER
  R&D Day: TICKER
  Capital Markets Day: TICKER
  Conference: TICKER

Multi-day events use Google's exclusive end-date convention (an event on
Sep 12 is start=2026-09-12, end=2026-09-13). Multi-day events from the
schema set end appropriately.

Idempotency: every event we create stores `analyst_days_event_id` in
`extendedProperties.private`. Update lookups use this ID rather than
title-matching, so two events on the same date for the same ticker
don't collide. The calendar event ID is also stored back into the
events.calendar_event_id column.

Auth:
  Local — GOOGLE_CREDENTIALS_PATH points at credentials.json (file path).
  CI — GOOGLE_CREDENTIALS_JSON contains the JSON blob as an env var.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger("analyst_days.gcal")

# Title prefixes — keep these stable so existing events don't get renamed
# accidentally on schema tweaks.
TITLE_PREFIX = {
    "investor_day": "Investor Day",
    "analyst_day": "Analyst Day",
    "rd_day": "R&D Day",
    "capital_markets_day": "Capital Markets Day",
    "conference": "Conference",
}

EVENT_TYPE_LABEL = {
    "investor_day": "Investor Day",
    "analyst_day": "Analyst Day",
    "rd_day": "R&D Day",
    "capital_markets_day": "Capital Markets Day",
    "conference": "Conference",
}

CAL_SCOPES = ["https://www.googleapis.com/auth/calendar"]
EXT_PROP_KEY = "analyst_days_event_id"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def get_service():
    """Build a Google Calendar API service from service-account credentials.

    Local: GOOGLE_CREDENTIALS_PATH points at the JSON file.
    CI: GOOGLE_CREDENTIALS_JSON contains the JSON content directly.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    blob = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if blob:
        info = json.loads(blob)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=CAL_SCOPES
        )
    else:
        path = os.environ.get("GOOGLE_CREDENTIALS_PATH")
        if not path or not os.path.exists(path):
            raise RuntimeError(
                "Google credentials not configured. Set GOOGLE_CREDENTIALS_PATH "
                "(local) or GOOGLE_CREDENTIALS_JSON (CI)."
            )
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=CAL_SCOPES
        )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _calendar_id() -> str:
    cid = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
    if not cid:
        raise RuntimeError("GOOGLE_CALENDAR_ID not set")
    return cid


# ---------------------------------------------------------------------------
# Event body builders
# ---------------------------------------------------------------------------


def _title(event_row) -> str:
    prefix = TITLE_PREFIX.get(event_row["event_type"], "Event")
    return f"{prefix}: {event_row['ticker']}"


def _description(event_row, source_url: Optional[str], rationale: Optional[str]) -> str:
    parts = []
    if event_row["company_name"]:
        parts.append(event_row["company_name"])
    parts.append(f"Type: {EVENT_TYPE_LABEL.get(event_row['event_type'], event_row['event_type'])}")
    if event_row["multi_day"] and event_row["end_date"]:
        parts.append(f"Multi-day: {event_row['start_date']} – {event_row['end_date']}")
    parts.append(f"Confidence: {event_row['confidence']:.2f}")
    parts.append(f"Status: {event_row['status']}")
    if source_url:
        parts.append(f"Source: {source_url}")
    if rationale:
        parts.append("")
        parts.append(rationale)
    parts.append("")
    parts.append("(Posted by analyst-days automation.)")
    return "\n".join(parts)


def _date_window(event_row) -> tuple[str, str]:
    """Return (start_date, end_date) using Google's exclusive-end-date convention.

    Single-day: end = start + 1 day
    Multi-day:  end = stored end_date + 1 day
    """
    start_iso = event_row["start_date"]
    if event_row["multi_day"] and event_row["end_date"]:
        end_iso = event_row["end_date"]
    else:
        end_iso = start_iso
    end = date.fromisoformat(end_iso) + timedelta(days=1)
    return start_iso, end.isoformat()


def _build_event_body(event_row, source_url: Optional[str], rationale: Optional[str]) -> dict:
    start_iso, end_iso = _date_window(event_row)
    return {
        "summary": _title(event_row),
        "description": _description(event_row, source_url, rationale),
        "start": {"date": start_iso},
        "end": {"date": end_iso},
        "extendedProperties": {
            "private": {
                EXT_PROP_KEY: str(event_row["id"]),
                "ticker": event_row["ticker"],
                "event_type": event_row["event_type"],
            },
        },
    }


# ---------------------------------------------------------------------------
# Public CRUD
# ---------------------------------------------------------------------------


def upsert_calendar_event(
    service,
    conn: sqlite3.Connection,
    event_row,
) -> str:
    """Create or update the calendar event for one analyst-days event row.

    Returns the Google Calendar event ID. Also persists it on
    events.calendar_event_id so subsequent runs update in place.

    Conferences are tracked but not pushed — caller should filter on
    PUSHABLE_EVENT_TYPES before calling this; this function will raise
    if asked to write a non-pushable event (defense in depth).
    """
    from src.state.events_repo import is_pushable

    if not is_pushable(event_row["event_type"]):
        raise ValueError(
            f"Refusing to post non-pushable event_type={event_row['event_type']!r} "
            "to Google Calendar"
        )
    if not event_row["start_date"]:
        raise ValueError("Cannot post imprecise event to Calendar — start_date is null")

    cal_id = _calendar_id()

    # Look up the source URL + rationale for the description
    src = conn.execute(
        "SELECT source_url, source_excerpt FROM event_sources "
        "WHERE event_id = ? ORDER BY id ASC LIMIT 1",
        (event_row["id"],),
    ).fetchone()
    source_url = src["source_url"] if src else None
    rationale = src["source_excerpt"] if src else None

    body = _build_event_body(event_row, source_url, rationale)

    existing_id = event_row["calendar_event_id"]
    if existing_id:
        try:
            service.events().update(
                calendarId=cal_id, eventId=existing_id, body=body
            ).execute()
            logger.info("gcal updated event_id=%s gcal_id=%s", event_row["id"], existing_id)
            return existing_id
        except Exception as e:
            # Fall through and re-create. Most common cause: event was deleted
            # from the calendar manually.
            logger.warning(
                "gcal update failed for event_id=%s gcal_id=%s; re-creating: %s",
                event_row["id"], existing_id, e,
            )

    # Create new
    created = service.events().insert(calendarId=cal_id, body=body).execute()
    new_gcal_id = created["id"]
    conn.execute(
        "UPDATE events SET calendar_event_id = ? WHERE id = ?",
        (new_gcal_id, event_row["id"]),
    )
    conn.commit()
    logger.info("gcal created event_id=%s gcal_id=%s", event_row["id"], new_gcal_id)
    return new_gcal_id


def delete_calendar_event(service, conn: sqlite3.Connection, event_id: int) -> bool:
    """Delete the calendar event for a row (idempotent — silent on 404)."""
    row = conn.execute(
        "SELECT calendar_event_id FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    if not row or not row["calendar_event_id"]:
        return False
    cal_id = _calendar_id()
    try:
        service.events().delete(
            calendarId=cal_id, eventId=row["calendar_event_id"]
        ).execute()
    except Exception as e:
        logger.warning("gcal delete failed for event_id=%s: %s", event_id, e)
        return False
    conn.execute(
        "UPDATE events SET calendar_event_id = NULL WHERE id = ?", (event_id,)
    )
    conn.commit()
    return True


def find_existing_by_event_id(service, analyst_days_event_id: int) -> Optional[str]:
    """Find a calendar event by our extended-property ID. Used to recover
    after a DB rebuild (CI artifact loss) — we can re-attach to existing
    calendar entries instead of creating duplicates.
    """
    cal_id = _calendar_id()
    resp = service.events().list(
        calendarId=cal_id,
        privateExtendedProperty=f"{EXT_PROP_KEY}={analyst_days_event_id}",
        maxResults=2,
        singleEvents=True,
    ).execute()
    items = resp.get("items", [])
    if not items:
        return None
    if len(items) > 1:
        logger.warning(
            "Multiple gcal events match analyst_days_event_id=%s; using first",
            analyst_days_event_id,
        )
    return items[0]["id"]


# ---------------------------------------------------------------------------
# Sanity test
# ---------------------------------------------------------------------------


def smoke_test():
    """Print calendar metadata. Verifies auth + calendar access without writing."""
    service = get_service()
    cal_id = _calendar_id()
    info = service.calendars().get(calendarId=cal_id).execute()
    print(f"Calendar OK: {info.get('summary')!r}")
    print(f"  id: {info.get('id')}")
    print(f"  timeZone: {info.get('timeZone')}")
