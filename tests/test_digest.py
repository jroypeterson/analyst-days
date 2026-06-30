"""Tests for the Monday email digest HTML rendering."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.state.schema import init_db
from src.state.events_repo import CandidateEvent, CandidateSource, upsert_event
from src.digest import query_monday, render_monday_html


def _confirmed(conn, ticker, start_date, **overrides):
    base = dict(
        ticker=ticker, company_name=f"{ticker} Inc.", event_type="investor_day",
        start_date=start_date, confidence=0.95, date_grounded=True,
        sources=[CandidateSource(source_type="8K", source_url=f"https://x/{ticker}")],
    )
    base.update(overrides)
    return upsert_event(conn, CandidateEvent(**base))


def test_query_monday_windows(tmp_path):
    conn = init_db(tmp_path / "events.db")
    today = "2026-01-01"
    _confirmed(conn, "WK", "2026-01-05")    # +4  -> in 7 and 30
    _confirmed(conn, "MO", "2026-01-20")    # +19 -> in 30 only
    _confirmed(conn, "FAR", "2026-03-01")   # +59 -> neither
    in_30, in_7 = query_monday(conn, today)
    assert [r["ticker"] for r in in_30] == ["WK", "MO"]
    assert [r["ticker"] for r in in_7] == ["WK"]


def test_render_monday_html(tmp_path):
    conn = init_db(tmp_path / "events.db")
    _confirmed(conn, "AFRM", "2026-01-05")
    subject, body, n30 = render_monday_html(conn, "2026-01-01")
    assert n30 == 1
    assert "Monday Outlook" in subject
    assert "<code>AFRM</code>" in body  # backtick-chip convention (#67)
    assert "Investor Day" in body


def test_render_empty(tmp_path):
    conn = init_db(tmp_path / "events.db")
    subject, body, n30 = render_monday_html(conn, "2026-01-01")
    assert n30 == 0
    assert "(none)" in body
