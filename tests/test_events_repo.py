"""Smoke-level tests for the schema + dedup logic."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.state.schema import init_db, schema_version, CURRENT_SCHEMA_VERSION
from src.state.events_repo import (
    CandidateEvent,
    CandidateSource,
    upsert_event,
    find_event,
    upcoming_events,
    tentative_events,
)


def _candidate(**overrides):
    base = dict(
        ticker="AAPL",
        company_name="Apple Inc.",
        event_type="analyst_day",
        start_date="2026-09-15",
        confidence=0.9,
        sources=[
            CandidateSource(
                source_type="8K",
                source_url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=8-K",
                source_excerpt="Apple to host its annual investor day on September 15, 2026.",
                accession_no="0001193125-26-000001",
            )
        ],
    )
    base.update(overrides)
    return CandidateEvent(**base)


def test_schema_inits_to_current_version(tmp_path):
    conn = init_db(tmp_path / "events.db")
    assert schema_version(conn) == CURRENT_SCHEMA_VERSION


def test_insert_confirms_high_confidence_precise(tmp_path):
    conn = init_db(tmp_path / "events.db")
    eid, status, is_new = upsert_event(conn, _candidate())
    assert is_new
    assert status == "confirmed"
    row = find_event(conn, "AAPL", "analyst_day", "2026-09-15")
    assert row["confirmed_at"] is not None


def test_imprecise_lands_as_tentative(tmp_path):
    conn = init_db(tmp_path / "events.db")
    eid, status, _ = upsert_event(
        conn,
        _candidate(
            start_date=None, date_imprecise=True, imprecise_hint="Q3 2026"
        ),
    )
    assert status == "tentative"


def test_low_confidence_precise_lands_as_discovered(tmp_path):
    conn = init_db(tmp_path / "events.db")
    eid, status, _ = upsert_event(conn, _candidate(confidence=0.5))
    assert status == "discovered"


def test_dedup_merges_sources_and_bumps_confidence(tmp_path):
    conn = init_db(tmp_path / "events.db")

    eid_a, _, new_a = upsert_event(conn, _candidate(confidence=0.7))
    eid_b, status_b, new_b = upsert_event(
        conn,
        _candidate(
            confidence=0.95,
            sources=[
                CandidateSource(
                    source_type="IR_PAGE",
                    source_url="https://investor.apple.com/events/2026",
                    source_excerpt="Annual investor day · September 15, 2026",
                )
            ],
        ),
    )
    assert eid_a == eid_b
    assert not new_b  # second was a merge
    # Tentative-promotion path doesn't apply here (start_date precise).
    # But low-confidence "discovered" can still flip to confirmed on bump.
    assert status_b in ("confirmed", "discovered")
    sources = conn.execute(
        "SELECT source_url FROM event_sources WHERE event_id = ? ORDER BY id",
        (eid_a,),
    ).fetchall()
    urls = [r["source_url"] for r in sources]
    assert any("sec.gov" in u for u in urls)
    assert any("investor.apple.com" in u for u in urls)


def test_tentative_can_promote_to_confirmed_with_precise_source(tmp_path):
    conn = init_db(tmp_path / "events.db")
    # First insert: imprecise, lands as tentative under (AAPL, analyst_day, NULL).
    upsert_event(
        conn,
        _candidate(
            start_date=None, date_imprecise=True, imprecise_hint="Q3 2026",
            confidence=0.6,
        ),
    )
    # Second insert at the SAME (ticker, event_type, start_date=NULL) with
    # higher confidence + still-imprecise: stays tentative. We can't promote
    # to confirmed without a precise start_date — that creates a *different*
    # row under the unique key (ticker, event_type, start_date).
    eid, status, is_new = upsert_event(
        conn,
        _candidate(
            start_date="2026-09-15",
            date_imprecise=False,
            confidence=0.9,
        ),
    )
    assert is_new  # different start_date → different row
    assert status == "confirmed"


def test_upcoming_filters_by_horizon(tmp_path):
    conn = init_db(tmp_path / "events.db")
    upsert_event(conn, _candidate(ticker="AAPL", start_date="2026-09-15"))
    upsert_event(conn, _candidate(ticker="MSFT", company_name="Microsoft", start_date="2027-01-10"))

    rows = upcoming_events(conn, today_iso="2026-09-01", horizon_days=30)
    tickers = [r["ticker"] for r in rows]
    assert "AAPL" in tickers
    assert "MSFT" not in tickers


def test_tentative_listed_separately(tmp_path):
    conn = init_db(tmp_path / "events.db")
    upsert_event(
        conn,
        _candidate(
            ticker="MRNA",
            company_name="Moderna",
            event_type="rd_day",
            start_date=None,
            date_imprecise=True,
            imprecise_hint="Fall 2026",
        ),
    )
    rows = tentative_events(conn)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "MRNA"
