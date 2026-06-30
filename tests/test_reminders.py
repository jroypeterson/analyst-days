"""Tests for the reminder state machine (T-30 / T-7 / day-of)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.state.schema import init_db
from src.state.events_repo import CandidateEvent, CandidateSource, upsert_event
from src.reminders import due_reminders, run_reminders


def _confirmed(conn, ticker, start_date, **overrides):
    base = dict(
        ticker=ticker,
        company_name=f"{ticker} Inc.",
        event_type="investor_day",
        start_date=start_date,
        confidence=0.95,  # >= 0.85 investor_day threshold -> confirmed
        date_grounded=True,  # date found in source text (see date-grounding gate)
        sources=[CandidateSource(source_type="8K", source_url=f"https://x/{ticker}")],
    )
    base.update(overrides)
    eid, status, _ = upsert_event(conn, CandidateEvent(**base))
    assert status == "confirmed", f"expected confirmed, got {status}"
    return eid


def test_window_bucketing(tmp_path):
    conn = init_db(tmp_path / "events.db")
    today = "2026-01-01"
    _confirmed(conn, "T30", "2026-01-20")   # +19 days -> t30
    _confirmed(conn, "T7", "2026-01-08")    # +7 days  -> t7
    _confirmed(conn, "DAYOF", "2026-01-04")  # +3 days  -> day_of
    _confirmed(conn, "FAR", "2026-03-01")    # +59 days -> none
    _confirmed(conn, "PAST", "2025-12-31")   # past     -> none

    due = {row["ticker"]: kind for row, kind, _, _ in due_reminders(conn, today)}
    assert due == {"T30": "t30", "T7": "t7", "DAYOF": "day_of"}


def test_one_shot_no_refire(tmp_path):
    conn = init_db(tmp_path / "events.db")
    today = "2026-01-01"
    _confirmed(conn, "T30", "2026-01-20")  # t30 window

    s1 = run_reminders(conn, today_iso=today, no_slack=True)
    assert s1["t30"] == 1 and s1["errors"] == 0
    # Status advanced + guard stamped.
    row = conn.execute("SELECT status, reminded_30_at FROM events").fetchone()
    assert row["status"] == "reminded_30"
    assert row["reminded_30_at"] is not None
    # Re-run same day: nothing due (one-shot).
    s2 = run_reminders(conn, today_iso=today, no_slack=True)
    assert s2["due"] == 0


def test_progression_across_weeks(tmp_path):
    conn = init_db(tmp_path / "events.db")
    _confirmed(conn, "X", "2026-02-01")
    # ~30 days out
    run_reminders(conn, today_iso="2026-01-05", no_slack=True)  # +27 -> t30
    assert conn.execute("SELECT status FROM events").fetchone()["status"] == "reminded_30"
    # ~1 week out
    s = run_reminders(conn, today_iso="2026-01-26", no_slack=True)  # +6? -> day_of window
    # 2026-01-26 -> 2026-02-01 = 6 days -> day_of bucket
    assert s["day_of"] == 1
    assert conn.execute("SELECT status FROM events").fetchone()["status"] == "day_of"


def test_tentative_not_reminded(tmp_path):
    conn = init_db(tmp_path / "events.db")
    upsert_event(conn, CandidateEvent(
        ticker="IMP", company_name="Imprecise Co", event_type="investor_day",
        start_date=None, date_imprecise=True, imprecise_hint="Q1 2026",
        confidence=0.95,
        sources=[CandidateSource(source_type="TAVILY_HIT", source_url="https://x/imp")],
    ))
    assert due_reminders(conn, "2026-01-01") == []
