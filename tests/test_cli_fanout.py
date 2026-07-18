"""Test the fan-out candidate floor: a confirmed, pushable row whose date has
passed must not be (re-)selected for Slack/Calendar/TickTick fan-out. This is
the defense-in-depth half of the past-date backstop (the other half stops such
a row confirming in the first place; this guards legacy/edge rows)."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.state.schema import init_db
from src.cli import _fanout_candidates


def _insert(conn, ticker, start_date, status="confirmed",
            event_type="analyst_day"):
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO events (ticker, company_name, event_type, start_date, "
        "multi_day, date_imprecise, status, confidence, first_seen_at, "
        "last_seen_at, confirmed_at) VALUES (?, ?, ?, ?, 0, 0, ?, 0.95, ?, ?, ?)",
        (ticker, f"{ticker} Inc.", event_type, start_date, status, now, now, now),
    )
    conn.commit()


def test_fanout_excludes_past_confirmed(tmp_path):
    conn = init_db(tmp_path / "events.db")
    today = date.today()
    future = (today + timedelta(days=10)).isoformat()
    past = (today - timedelta(days=1)).isoformat()

    _insert(conn, "FUT", future, status="confirmed")
    _insert(conn, "PAST", past, status="confirmed")

    rows = _fanout_candidates(conn, today.isoformat())
    tickers = {r["ticker"] for r in rows}
    assert "FUT" in tickers
    assert "PAST" not in tickers


def test_fanout_includes_today(tmp_path):
    conn = init_db(tmp_path / "events.db")
    today = date.today().isoformat()
    _insert(conn, "NOW", today, status="confirmed")
    rows = _fanout_candidates(conn, today)
    assert {r["ticker"] for r in rows} == {"NOW"}
