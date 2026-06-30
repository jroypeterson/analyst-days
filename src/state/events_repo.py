"""
Repository functions for events + event_sources.

Dedup rule: a candidate event is a duplicate if (ticker, event_type, start_date)
already exists. On duplicate, source rows are merged (ON CONFLICT DO NOTHING on
(event_id, source_url)) and last_seen_at is bumped. Confidence is taken as the
max of stored vs. incoming.

Confidence thresholds are per-event-type:
  - investor / analyst / R&D / capital markets days: 0.85 (high bar; these are
    headline events that drive prep and shouldn't be auto-confirmed on weak
    signal)
  - conferences: 0.70 (consistently rated lower because Tavily snippets are
    the typical signal — failure mode is an extra calendar entry, not a
    wrong-date confirmation)
  - default for unknown types: 0.80
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


EVENT_TYPE_THRESHOLDS: dict[str, float] = {
    "investor_day": 0.85,
    "analyst_day": 0.85,
    "rd_day": 0.85,
    "capital_markets_day": 0.85,
    "conference": 0.70,
}
DEFAULT_CONFIDENCE_THRESHOLD = 0.80


# Event types that get pushed to Slack / Google Calendar / TickTick.
# Conferences are intentionally excluded — we track them in the DB (visible
# via --status) but they don't generate per-event pings, calendar entries,
# or digest table rows. The user's view: only analyst-style days drive prep.
PUSHABLE_EVENT_TYPES: set[str] = {
    "investor_day",
    "analyst_day",
    "rd_day",
    "capital_markets_day",
}


def is_pushable(event_type: str) -> bool:
    return event_type in PUSHABLE_EVENT_TYPES


# Source-sensitive confirmation bar: a marquee (pushable) event may only
# auto-confirm if it has at least one AUTHORITATIVE source — an 8-K, a company
# IR page, a press release, or a manual entry. A generic web hit (TAVILY_HIT —
# i.e. not on the company's own IR domain and not a PR wire) is too weak to
# single-source-confirm a prep-driving event; it stays tentative until an
# authoritative source corroborates. Conferences (tracked-only, never fanned
# out) are exempt — Tavily snippets are their normal signal.
AUTHORITATIVE_SOURCE_TYPES: set[str] = {"8K", "IR_PAGE", "PRESS_RELEASE", "MANUAL"}


def _is_authoritative(source_type: Optional[str]) -> bool:
    return source_type in AUTHORITATIVE_SOURCE_TYPES


def event_has_authoritative_source(conn: sqlite3.Connection, event_id: int) -> bool:
    placeholders = ",".join(["?"] * len(AUTHORITATIVE_SOURCE_TYPES))
    row = conn.execute(
        f"SELECT 1 FROM event_sources WHERE event_id = ? "
        f"AND source_type IN ({placeholders}) LIMIT 1",
        (event_id, *sorted(AUTHORITATIVE_SOURCE_TYPES)),
    ).fetchone()
    return row is not None


def threshold_for(event_type: str) -> float:
    return EVENT_TYPE_THRESHOLDS.get(event_type, DEFAULT_CONFIDENCE_THRESHOLD)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CandidateSource:
    source_type: str          # 8K | IR_PAGE | PRESS_RELEASE | TAVILY_HIT | MANUAL
    source_url: Optional[str] = None
    source_excerpt: Optional[str] = None
    accession_no: Optional[str] = None


@dataclass
class CandidateEvent:
    ticker: str
    company_name: Optional[str]
    event_type: str            # investor_day | analyst_day | rd_day | capital_markets_day | conference
    start_date: Optional[str]  # ISO YYYY-MM-DD; None if fully imprecise
    end_date: Optional[str] = None
    multi_day: bool = False
    date_imprecise: bool = False
    imprecise_hint: Optional[str] = None
    confidence: float = 0.0
    # Whether the precise start_date was found in the RAW source text (not the
    # classifier's rationale). A precise, high-confidence event whose date isn't
    # grounded stays tentative — see src/discovery/date_grounding.py and the
    # "date-grounding gate" notes in CLAUDE.md.
    date_grounded: bool = False
    sources: list[CandidateSource] = field(default_factory=list)


def find_event(
    conn: sqlite3.Connection,
    ticker: str,
    event_type: str,
    start_date: Optional[str],
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE ticker = ? AND event_type = ? "
        "AND ((? IS NULL AND start_date IS NULL) OR start_date = ?) LIMIT 1",
        (ticker, event_type, start_date, start_date),
    ).fetchone()


def upsert_event(
    conn: sqlite3.Connection,
    candidate: CandidateEvent,
    confidence_threshold: Optional[float] = None,
) -> tuple[int, str, bool]:
    """Insert or merge a candidate event.

    `confidence_threshold` defaults to the per-event-type threshold from
    EVENT_TYPE_THRESHOLDS; pass an explicit value to override (mostly useful
    in tests).

    Returns (event_id, status, is_new) where:
      - event_id: int
      - status: the resulting events.status value
      - is_new: True if this insert created a new event row, False on merge
    """
    now = _utcnow()
    threshold = (
        confidence_threshold
        if confidence_threshold is not None
        else threshold_for(candidate.event_type)
    )
    existing = find_event(
        conn, candidate.ticker, candidate.event_type, candidate.start_date
    )

    # Source-sensitive bar (pushable types only): a marquee event needs an
    # authoritative source to confirm; a generic Tavily-only hit stays tentative.
    requires_auth = is_pushable(candidate.event_type)
    cand_auth = any(_is_authoritative(s.source_type) for s in candidate.sources)

    if existing:
        new_confidence = max(existing["confidence"] or 0.0, candidate.confidence)
        new_status = existing["status"]
        # Grounding is sticky + accretive: once any source has grounded the
        # date, it stays grounded; a new corroborating source can also flip a
        # previously-ungrounded event to grounded.
        new_grounded = bool(existing["date_grounded"]) or candidate.date_grounded
        # Authoritative-source presence is likewise accretive across sources.
        has_auth = cand_auth or event_has_authoritative_source(conn, existing["id"])
        # Promote on merge: discovered/tentative -> confirmed when the combined
        # confidence clears the per-type bar AND the date is precise AND grounded
        # in raw source text (wrong-date guard) AND, for pushable types, backed by
        # an authoritative source (weak-source guard).
        if (
            existing["status"] in ("discovered", "tentative")
            and not candidate.date_imprecise
            and candidate.start_date
            and new_confidence >= threshold
            and new_grounded
            and (not requires_auth or has_auth)
        ):
            new_status = "confirmed"

        conn.execute(
            "UPDATE events SET last_seen_at = ?, confidence = ?, status = ?, "
            "company_name = COALESCE(?, company_name), "
            "end_date = COALESCE(?, end_date), "
            "multi_day = ?, "
            "imprecise_hint = COALESCE(?, imprecise_hint), "
            "date_grounded = ?, "
            "confirmed_at = COALESCE(confirmed_at, ?) "
            "WHERE id = ?",
            (
                now,
                new_confidence,
                new_status,
                candidate.company_name,
                candidate.end_date,
                int(candidate.multi_day or existing["multi_day"]),
                candidate.imprecise_hint,
                int(new_grounded),
                now if new_status == "confirmed" else None,
                existing["id"],
            ),
        )
        event_id = int(existing["id"])
        is_new = False
    else:
        precise = not candidate.date_imprecise and bool(candidate.start_date)
        auth_ok = (not requires_auth) or cand_auth
        if not precise:
            status = "tentative"
        elif candidate.confidence >= threshold and candidate.date_grounded and auth_ok:
            status = "confirmed"
        elif candidate.confidence >= threshold:
            # Precise + high confidence, but held at tentative (radar-only)
            # because EITHER the date isn't grounded in raw source text (likely a
            # mis-transcribed date) OR the only source is a generic web hit that
            # can't single-source-confirm a prep-driving event. A grounded,
            # authoritative corroboration later promotes it.
            status = "tentative"
        else:
            status = "discovered"

        cur = conn.execute(
            "INSERT INTO events ("
            "ticker, company_name, event_type, start_date, end_date, "
            "multi_day, date_imprecise, imprecise_hint, status, confidence, "
            "date_grounded, first_seen_at, last_seen_at, confirmed_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate.ticker,
                candidate.company_name,
                candidate.event_type,
                candidate.start_date,
                candidate.end_date,
                int(bool(candidate.multi_day)),
                int(bool(candidate.date_imprecise)),
                candidate.imprecise_hint,
                status,
                candidate.confidence,
                int(bool(candidate.date_grounded)),
                now,
                now,
                now if status == "confirmed" else None,
            ),
        )
        event_id = int(cur.lastrowid)
        new_status = status
        is_new = True

    for src in candidate.sources:
        # Source rows are deduped by (event_id, source_url). NULL urls are
        # always inserted (we shouldn't have many).
        if src.source_url is None:
            conn.execute(
                "INSERT INTO event_sources ("
                "event_id, source_type, source_url, source_excerpt, "
                "accession_no, retrieved_at) VALUES (?, ?, NULL, ?, ?, ?)",
                (event_id, src.source_type, src.source_excerpt,
                 src.accession_no, now),
            )
        else:
            conn.execute(
                "INSERT INTO event_sources ("
                "event_id, source_type, source_url, source_excerpt, "
                "accession_no, retrieved_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_id, source_url) DO NOTHING",
                (event_id, src.source_type, src.source_url,
                 src.source_excerpt, src.accession_no, now),
            )

    conn.commit()
    return event_id, new_status, is_new


def upcoming_events(
    conn: sqlite3.Connection,
    today_iso: str,
    horizon_days: int = 30,
) -> list[sqlite3.Row]:
    """Return confirmed events between today and today+horizon_days, ordered."""
    return conn.execute(
        "SELECT * FROM events WHERE status IN ('confirmed','reminded_30','reminded_7','day_of') "
        "AND start_date >= ? AND start_date <= date(?, ? || ' days') "
        "ORDER BY start_date ASC",
        (today_iso, today_iso, f"+{int(horizon_days)}"),
    ).fetchall()


def tentative_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE status = 'tentative' "
        "ORDER BY ticker ASC"
    ).fetchall()


def event_sources(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM event_sources WHERE event_id = ? ORDER BY retrieved_at ASC",
        (event_id,),
    ).fetchall()


def recompute_statuses(conn: sqlite3.Connection) -> int:
    """Walk all events and promote discovered/tentative -> confirmed where
    the stored confidence + per-type threshold now warrants it.

    Idempotent. Promotion-only (never demotes) so events that have already
    been fanned out stay confirmed even if you tighten thresholds later.

    Returns count of events promoted.
    """
    now = _utcnow()
    rows = conn.execute(
        "SELECT id, event_type, confidence, status, start_date, date_imprecise, "
        "date_grounded "
        "FROM events WHERE status IN ('discovered','tentative')"
    ).fetchall()
    promoted = 0
    for r in rows:
        threshold = threshold_for(r["event_type"])
        precise = bool(r["start_date"]) and not r["date_imprecise"]
        grounded = bool(r["date_grounded"])
        auth_ok = (not is_pushable(r["event_type"])) or \
            event_has_authoritative_source(conn, r["id"])
        if precise and grounded and auth_ok and (r["confidence"] or 0.0) >= threshold:
            conn.execute(
                "UPDATE events SET status = 'confirmed', "
                "confirmed_at = COALESCE(confirmed_at, ?) "
                "WHERE id = ?",
                (now, r["id"]),
            )
            promoted += 1
    if promoted:
        conn.commit()
    return promoted


def mark_status(
    conn: sqlite3.Connection, event_id: int, new_status: str, ts_field: str | None = None
) -> None:
    if ts_field:
        conn.execute(
            f"UPDATE events SET status = ?, {ts_field} = ? WHERE id = ?",
            (new_status, _utcnow(), event_id),
        )
    else:
        conn.execute(
            "UPDATE events SET status = ? WHERE id = ?", (new_status, event_id)
        )
    conn.commit()


_RETIRE_STATUSES = ("cancelled", "superseded")


def retire_event(
    conn: sqlite3.Connection,
    event_id: int,
    new_status: str = "cancelled",
    reason: Optional[str] = None,
) -> bool:
    """Retire an event to a terminal state without deleting the row.

    Use this to fix a wrong-date confirm (`superseded`) or record that an event
    was called off (`cancelled`) while preserving provenance. recompute_statuses()
    never reconsiders these rows, and the export filter hides them, so a retired
    event drops off the calendar/digest surfaces on the next fan-out/export.
    The Calendar/TickTick entries themselves are removed by the caller (see
    cmd_retire) — this only updates DB state.

    Returns True if a row was updated.
    """
    if new_status not in _RETIRE_STATUSES:
        raise ValueError(
            f"retire status must be one of {_RETIRE_STATUSES}, got {new_status!r}"
        )
    stamp = f"[{_utcnow()}] retired -> {new_status}"
    if reason:
        stamp += f": {reason}"
    cur = conn.execute(
        "UPDATE events SET status = ?, "
        "notes = TRIM(COALESCE(notes || char(10), '') || ?) "
        "WHERE id = ?",
        (new_status, stamp, event_id),
    )
    conn.commit()
    return cur.rowcount > 0
