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
    retire_event,
    upcoming_events,
    tentative_events,
)
import pytest


def _candidate(**overrides):
    base = dict(
        ticker="AAPL",
        company_name="Apple Inc.",
        event_type="analyst_day",
        start_date="2026-09-15",
        confidence=0.9,
        # Default to grounded — the discovery layer (cli._to_candidate) computes
        # this against raw source text; the repo just trusts the flag. Tests that
        # exercise the wrong-date guard pass date_grounded=False explicitly.
        date_grounded=True,
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


def test_retire_event_sets_terminal_status_and_note(tmp_path):
    conn = init_db(tmp_path / "events.db")
    eid, status, _ = upsert_event(conn, _candidate(start_date="2026-09-15"))
    assert status == "confirmed"

    assert retire_event(conn, eid, new_status="superseded", reason="wrong date") is True
    row = find_event(conn, "AAPL", "analyst_day", "2026-09-15")
    assert row["status"] == "superseded"
    assert "superseded" in (row["notes"] or "")
    assert "wrong date" in (row["notes"] or "")


def test_retire_event_rejects_non_terminal_status(tmp_path):
    conn = init_db(tmp_path / "events.db")
    eid, _, _ = upsert_event(conn, _candidate(start_date="2026-09-15"))
    with pytest.raises(ValueError):
        retire_event(conn, eid, new_status="confirmed")


def test_ungrounded_precise_high_confidence_stays_tentative(tmp_path):
    """The wrong-date guard: a precise, high-confidence date that isn't grounded
    in source text must NOT auto-confirm."""
    conn = init_db(tmp_path / "events.db")
    eid, status, _ = upsert_event(
        conn, _candidate(confidence=0.95, date_grounded=False)
    )
    assert status == "tentative"
    row = find_event(conn, "AAPL", "analyst_day", "2026-09-15")
    assert row["confirmed_at"] is None
    assert row["date_grounded"] == 0


def test_grounded_corroboration_promotes_ungrounded_event(tmp_path):
    """An ungrounded tentative event is promoted when a later, grounded source
    corroborates the same (ticker, type, date)."""
    conn = init_db(tmp_path / "events.db")
    upsert_event(conn, _candidate(confidence=0.95, date_grounded=False))
    # Second sighting of the SAME event, now grounded.
    eid, status, is_new = upsert_event(
        conn, _candidate(confidence=0.95, date_grounded=True)
    )
    assert is_new is False  # merge, same (ticker, type, start_date)
    assert status == "confirmed"
    row = find_event(conn, "AAPL", "analyst_day", "2026-09-15")
    assert row["date_grounded"] == 1


def test_recompute_skips_ungrounded(tmp_path):
    """recompute_statuses must not promote an ungrounded event even at high
    confidence."""
    from src.state.events_repo import recompute_statuses

    conn = init_db(tmp_path / "events.db")
    upsert_event(conn, _candidate(confidence=0.95, date_grounded=False))
    promoted = recompute_statuses(conn)
    assert promoted == 0
    row = find_event(conn, "AAPL", "analyst_day", "2026-09-15")
    assert row["status"] == "tentative"


def _tavily_only(**overrides):
    """A precise, grounded, high-confidence candidate whose ONLY source is a
    generic web hit (not authoritative)."""
    base = dict(
        sources=[CandidateSource(source_type="TAVILY_HIT",
                                 source_url="https://news.example/x")],
    )
    base.update(overrides)
    return _candidate(**base)


def test_pushable_tavily_only_stays_tentative(tmp_path):
    """Source-sensitive bar: a marquee event backed only by a generic web hit
    must NOT auto-confirm even when precise, grounded, and high-confidence."""
    conn = init_db(tmp_path / "events.db")
    _eid, status, _ = upsert_event(conn, _tavily_only(event_type="investor_day"))
    assert status == "tentative"


def test_conference_tavily_only_confirms(tmp_path):
    """Conferences are exempt from the authoritative-source bar (tracked-only,
    never fanned out; Tavily is their normal signal)."""
    conn = init_db(tmp_path / "events.db")
    _eid, status, _ = upsert_event(
        conn, _tavily_only(event_type="conference", confidence=0.75)
    )
    assert status == "confirmed"


def test_authoritative_corroboration_promotes_tavily_only(tmp_path):
    """A web-only tentative marquee event is promoted when an 8-K corroborates."""
    conn = init_db(tmp_path / "events.db")
    upsert_event(conn, _tavily_only(event_type="rd_day"))
    _eid, status, is_new = upsert_event(
        conn,
        _candidate(
            event_type="rd_day",
            sources=[CandidateSource(source_type="8K", source_url="https://sec.gov/8k")],
        ),
    )
    assert is_new is False
    assert status == "confirmed"


def test_recompute_skips_tavily_only_pushable(tmp_path):
    from src.state.events_repo import recompute_statuses

    conn = init_db(tmp_path / "events.db")
    upsert_event(conn, _tavily_only(event_type="analyst_day"))
    assert recompute_statuses(conn) == 0
    row = find_event(conn, "AAPL", "analyst_day", "2026-09-15")
    assert row["status"] == "tentative"


def test_past_precise_grounded_event_does_not_confirm(tmp_path):
    """Past-date backstop: a precise, grounded, high-confidence, authoritatively
    sourced date that is already in the past must NOT auto-confirm (it would
    otherwise fan out to Slack/Calendar/TickTick for an event that's over)."""
    conn = init_db(tmp_path / "events.db")
    # start_date 2026-09-15 with 'today' pinned AFTER it.
    eid, status, _ = upsert_event(conn, _candidate(), today_iso="2026-10-01")
    assert status == "tentative"
    row = find_event(conn, "AAPL", "analyst_day", "2026-09-15")
    assert row["confirmed_at"] is None
    # Sanity: the same candidate with 'today' before the date DOES confirm.
    conn2 = init_db(tmp_path / "events2.db")
    _eid, status2, _ = upsert_event(conn2, _candidate(), today_iso="2026-01-01")
    assert status2 == "confirmed"


def test_merge_does_not_confirm_past_event(tmp_path):
    """A tentative row whose date has since passed must not promote on merge."""
    conn = init_db(tmp_path / "events.db")
    upsert_event(conn, _candidate(confidence=0.95, date_grounded=False),
                 today_iso="2026-01-01")
    _eid, status, is_new = upsert_event(
        conn, _candidate(confidence=0.95, date_grounded=True),
        today_iso="2026-10-01",
    )
    assert is_new is False
    assert status == "tentative"


def test_recompute_skips_past_event(tmp_path):
    """recompute_statuses must not promote an event whose date is now past."""
    from src.state.events_repo import recompute_statuses

    conn = init_db(tmp_path / "events.db")
    # Land it tentative by pinning today before the date but withholding grounding.
    upsert_event(conn, _candidate(confidence=0.95, date_grounded=False),
                 today_iso="2026-01-01")
    # Now ground it via a merge, but keep it tentative because today is past it.
    upsert_event(conn, _candidate(confidence=0.95, date_grounded=True),
                 today_iso="2026-10-01")
    assert recompute_statuses(conn, today_iso="2026-10-01") == 0
    row = find_event(conn, "AAPL", "analyst_day", "2026-09-15")
    assert row["status"] == "tentative"


def test_precise_row_supersedes_imprecise_null_sibling(tmp_path):
    """When a precise date arrives, the leftover imprecise NULL-date placeholder
    for the same (ticker, event_type) is retired (superseded), so the Friday
    Radar no longer double-shows the event."""
    conn = init_db(tmp_path / "events.db")
    # First: imprecise NULL row -> tentative.
    null_eid, _, _ = upsert_event(
        conn,
        _candidate(start_date=None, date_imprecise=True, imprecise_hint="Q3 2026"),
    )
    # Then: precise date arrives (different row under the unique key).
    precise_eid, _, is_new = upsert_event(conn, _candidate(start_date="2026-09-15"))
    assert is_new  # different start_date → different row
    assert precise_eid != null_eid

    null_row = conn.execute(
        "SELECT status, notes FROM events WHERE id = ?", (null_eid,)
    ).fetchone()
    assert null_row["status"] == "superseded"
    assert "superseded by precise-dated row" in (null_row["notes"] or "")
    # The precise row itself is untouched.
    precise_row = conn.execute(
        "SELECT status FROM events WHERE id = ?", (precise_eid,)
    ).fetchone()
    assert precise_row["status"] != "superseded"


def test_recompute_never_revives_retired_event(tmp_path):
    """A retired (terminal) event must not be re-promoted by recompute_statuses."""
    from src.state.events_repo import recompute_statuses

    conn = init_db(tmp_path / "events.db")
    eid, _, _ = upsert_event(conn, _candidate(start_date="2026-09-15", confidence=0.95))
    retire_event(conn, eid, new_status="cancelled")
    recompute_statuses(conn)
    row = find_event(conn, "AAPL", "analyst_day", "2026-09-15")
    assert row["status"] == "cancelled"
