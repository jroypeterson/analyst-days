"""
Repository functions for events + event_sources.

Dedup rule: a candidate event is a duplicate if (ticker, event_type, start_date)
already exists. On duplicate, source rows are merged (ON CONFLICT DO NOTHING on
(event_id, source_url)) and last_seen_at is bumped. Confidence is taken as the
max of stored vs. incoming.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


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
    confidence_threshold: float = 0.80,
) -> tuple[int, str, bool]:
    """Insert or merge a candidate event.

    Returns (event_id, status, is_new) where:
      - event_id: int
      - status: the resulting events.status value
      - is_new: True if this insert created a new event row, False on merge
    """
    now = _utcnow()
    existing = find_event(
        conn, candidate.ticker, candidate.event_type, candidate.start_date
    )

    if existing:
        new_confidence = max(existing["confidence"] or 0.0, candidate.confidence)
        new_status = existing["status"]
        # An imprecise tentative can be promoted to confirmed if a precise
        # corroborating source arrives.
        if (
            existing["status"] == "tentative"
            and not candidate.date_imprecise
            and candidate.start_date
            and new_confidence >= confidence_threshold
        ):
            new_status = "confirmed"

        conn.execute(
            "UPDATE events SET last_seen_at = ?, confidence = ?, status = ?, "
            "company_name = COALESCE(?, company_name), "
            "end_date = COALESCE(?, end_date), "
            "multi_day = ?, "
            "imprecise_hint = COALESCE(?, imprecise_hint), "
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
                now if new_status == "confirmed" else None,
                existing["id"],
            ),
        )
        event_id = int(existing["id"])
        is_new = False
    else:
        if candidate.date_imprecise or candidate.start_date is None:
            status = "tentative"
        elif candidate.confidence >= confidence_threshold:
            status = "confirmed"
        else:
            status = "discovered"

        cur = conn.execute(
            "INSERT INTO events ("
            "ticker, company_name, event_type, start_date, end_date, "
            "multi_day, date_imprecise, imprecise_hint, status, confidence, "
            "first_seen_at, last_seen_at, confirmed_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
