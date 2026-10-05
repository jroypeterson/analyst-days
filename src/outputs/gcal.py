"""Google Calendar output.

Writes confirmed events to the dedicated "Other Investing" calendar in
floridabusinessman@gmail.com (set via GOOGLE_CALENDAR_ID; split off the
legacy shared earnings calendar on 2026-05-28). Auth is the shared
earnings-agent service account. Each analyst-days event is an ALL-DAY block
with a type-prefixed title:

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
from dataclasses import dataclass
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
# Stored as a private extended property so we can recover/re-attach to an
# existing calendar entry after a DB rebuild (CI artifact loss). Must be a
# STABLE natural key, not the SQLite row id — AUTOINCREMENT ids are not
# preserved when the DB is rebuilt from discovery, which would defeat recovery.
EXT_PROP_KEY = "analyst_days_event_key"


def _event_key(event_row) -> str:
    """Deterministic natural key for an event: stable across DB rebuilds.

    Keyed the same way as the DB dedup unique constraint
    (ticker, event_type, start_date), with end_date appended so a single-day ->
    multi-day correction is a distinct calendar entry.
    """
    end = event_row["end_date"] or event_row["start_date"]
    return "|".join((
        (event_row["ticker"] or "").upper(),
        event_row["event_type"] or "",
        event_row["start_date"] or "",
        end or "",
    ))


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
                EXT_PROP_KEY: _event_key(event_row),
                "ticker": event_row["ticker"],
                "event_type": event_row["event_type"],
            },
        },
    }


# ---------------------------------------------------------------------------
# Public CRUD
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushResult:
    """What fan-out did for one row. `action` is one of:
    created          -- a new calendar event was inserted
    updated          -- the stored id was updated in place
    adopted          -- no stored id, but a LIVE event with our natural key already
                        existed (DB rebuilt, board #456) -> re-attached, not duplicated
    cancelled_match  -- no stored id and the only event with our key is cancelled/
                        trashed: it was fanned out before and then removed (`--retire`
                        or by hand). Nothing inserted; the id is NOT stored (updating a
                        cancelled event is undefined). The caller treats this as
                        evidence the row was retired and skips TickTick + Slack too.
    """
    gcal_id: Optional[str]
    action: str


def _http_status(exc: Exception) -> Optional[int]:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def upsert_calendar_event(
    service,
    conn: sqlite3.Connection,
    event_row,
) -> PushResult:
    """Create, update or re-attach the calendar event for one analyst-days row.

    Persists the Google id on events.calendar_event_id so later runs update in
    place. NEVER blind-inserts: a row without a stored id is first looked up by
    its natural key (`find_existing_event`), and a stored id that fails to update
    is re-created only when Google says it is gone (404/410) -- any other error
    propagates and is counted by the caller, because an insert we cannot prove
    is unique is how a rebuilt DB doubled the calendar (board #456).

    Conferences are tracked but not pushed -- caller should filter on
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

    def _persist(gcal_id: str) -> None:
        conn.execute(
            "UPDATE events SET calendar_event_id = ? WHERE id = ?",
            (gcal_id, event_row["id"]),
        )
        conn.commit()

    existing_id = event_row["calendar_event_id"]
    if existing_id:
        try:
            service.events().update(
                calendarId=cal_id, eventId=existing_id, body=body
            ).execute()
            logger.info("gcal updated event_id=%s gcal_id=%s", event_row["id"], existing_id)
            return PushResult(existing_id, "updated")
        except Exception as e:
            if _http_status(e) not in (404, 410):
                raise  # transient / auth / quota: retry next run, never duplicate
            logger.warning(
                "gcal event gone for event_id=%s gcal_id=%s; looking up by key: %s",
                event_row["id"], existing_id, e,
            )

    # No usable stored id: re-attach before creating. A lookup error propagates.
    found = find_existing_event(service, event_row)
    if found is not None:
        if found.get("status") == "cancelled":
            logger.warning(
                "gcal: only a CANCELLED event carries key %s for event_id=%s; "
                "treating as retired, not re-creating",
                _event_key(event_row), event_row["id"],
            )
            return PushResult(None, "cancelled_match")
        service.events().update(
            calendarId=cal_id, eventId=found["id"], body=body
        ).execute()
        _persist(found["id"])
        logger.info("gcal adopted event_id=%s gcal_id=%s", event_row["id"], found["id"])
        return PushResult(found["id"], "adopted")

    created = service.events().insert(calendarId=cal_id, body=body).execute()
    new_gcal_id = created["id"]
    _persist(new_gcal_id)
    logger.info("gcal created event_id=%s gcal_id=%s", event_row["id"], new_gcal_id)
    return PushResult(new_gcal_id, "created")


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


def find_existing_event(service, event_row) -> Optional[dict]:
    """Find the calendar event carrying our deterministic extended-property key.

    This is how fan-out survives a DB rebuild (CI artifact loss, board #456):
    AUTOINCREMENT ids and the stored Google ids are gone, but the natural key
    (`_event_key`) is not. `showDeleted=True` so an entry JP deleted by hand or
    `--retire` removed is still seen: a cancelled match means "fanned out
    before, then removed", and the caller must not resurrect it. A live match is
    preferred over a cancelled one. Note Google purges trashed events after
    ~30 days, after which a retired event rediscovered by a rebuilt DB WILL be
    re-created -- the window is bounded, not closed.

    Returns the event resource (at least `id` and `status`) or None. Any API
    error propagates: an insert we cannot prove is unique is the defect.
    """
    cal_id = _calendar_id()
    key = _event_key(event_row)
    resp = service.events().list(
        calendarId=cal_id,
        privateExtendedProperty=f"{EXT_PROP_KEY}={key}",
        maxResults=10,
        singleEvents=True,
        showDeleted=True,
    ).execute()
    items = resp.get("items", []) or []
    if not items:
        return None
    live = [it for it in items if it.get("status") != "cancelled"]
    if len(live) > 1:
        logger.warning(
            "Multiple live gcal events match %s=%s; using first", EXT_PROP_KEY, key,
        )
    return live[0] if live else items[0]


def find_existing_by_event_key(service, event_row) -> Optional[str]:
    """Back-compat shim: the id of the LIVE event with our key, or None."""
    found = find_existing_event(service, event_row)
    if found is None or found.get("status") == "cancelled":
        return None
    return found["id"]


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
