"""Tests for the sa-monitor export filter (confirmed + start_date >= today).

The export is a cross-project contract: sa-monitor's AnalystDayCalendar reads it
by (ticker, halt_date) and renders a "hosting an analyst day today" Note for any
match WITHOUT re-checking status — so the export must publish only confirmed,
still-future rows. It must also keep schema_version=1 and the full field set.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from src.state.schema import init_db  # noqa: E402
import export_upcoming_events as exp  # noqa: E402


def _insert(conn, *, ticker, event_type, start_date, status,
            company_name="X Corp", multi_day=0, end_date=None):
    """Insert a fully-formed event row directly (bypasses upsert gating so we
    can seed statuses the confirmation logic itself would now refuse — e.g. a
    legacy past 'confirmed' row)."""
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO events (ticker, company_name, event_type, start_date, "
        "end_date, multi_day, date_imprecise, imprecise_hint, status, "
        "confidence, first_seen_at, last_seen_at, confirmed_at) VALUES "
        "(?, ?, ?, ?, ?, ?, 0, NULL, ?, 0.95, ?, ?, ?)",
        (ticker, company_name, event_type, start_date, end_date, multi_day,
         status, now, now, now),
    )
    conn.commit()


def _run_export(tmp_path, conn):
    out = tmp_path / "upcoming_events.json"
    n = exp.export(tmp_path / "events.db", out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    return n, payload


def test_export_publishes_only_confirmed_future(tmp_path):
    db = tmp_path / "events.db"
    conn = init_db(db)
    today = date.today()
    future = (today + timedelta(days=10)).isoformat()
    past = (today - timedelta(days=10)).isoformat()

    _insert(conn, ticker="AAA", event_type="analyst_day",
            start_date=future, status="confirmed")           # keep
    _insert(conn, ticker="BBB", event_type="investor_day",
            start_date=future, status="tentative")           # drop (unconfirmed)
    _insert(conn, ticker="CCC", event_type="analyst_day",
            start_date=future, status="discovered")          # drop (unconfirmed)
    _insert(conn, ticker="DDD", event_type="analyst_day",
            start_date=past, status="confirmed")             # drop (past)
    _insert(conn, ticker="EEE", event_type="analyst_day",
            start_date=future, status="cancelled")           # drop (terminal)
    conn.close()

    n, payload = _run_export(tmp_path, conn)
    tickers = {e["ticker"] for e in payload["events"]}
    assert tickers == {"AAA"}
    assert n == 1


def test_export_keeps_confirmed_conference(tmp_path):
    """Conferences reach 'confirmed' in the DB and sa-monitor renders a note for
    them — they must survive the filter when future + confirmed."""
    db = tmp_path / "events.db"
    conn = init_db(db)
    future = (date.today() + timedelta(days=5)).isoformat()
    _insert(conn, ticker="CONF", event_type="conference",
            start_date=future, status="confirmed")
    conn.close()

    _n, payload = _run_export(tmp_path, conn)
    assert [e["ticker"] for e in payload["events"]] == ["CONF"]


def test_export_contract_schema_and_fields(tmp_path):
    """schema_version stays 1 and every field sa-monitor reads is present."""
    db = tmp_path / "events.db"
    conn = init_db(db)
    future = (date.today() + timedelta(days=3)).isoformat()
    _insert(conn, ticker="ZZZ", event_type="analyst_day",
            start_date=future, status="confirmed", company_name="Zeta Inc.")
    conn.close()

    _n, payload = _run_export(tmp_path, conn)
    assert payload["schema_version"] == 1
    assert payload["source"] == "analyst-days"
    ev = payload["events"][0]
    for field in ("ticker", "company_name", "event_type", "start_date",
                  "end_date", "multi_day", "status"):
        assert field in ev
    assert ev["ticker"] == "ZZZ"
    assert ev["status"] == "confirmed"
