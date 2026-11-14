"""The bounded historical backfill (2026-11-14): past-events mode, `status='historical'`,
and the two things it must never do -- fan out, or write the production database.

No network and no model: `scan_universe` gets a fake `get`, the classifier is a fake
callable, and every database is under `tmp_path`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src import backfill
from src.discovery import classify as classify_mod
from src.discovery import scan_edgar_fts as fts
from src.discovery.date_grounding import grounded_in_cited_source
from src.reminders import due_reminders
from src.state import events_repo
from src.state.events_repo import (
    CandidateEvent, CandidateSource, recompute_statuses, tentative_events,
    upcoming_events, upsert_event,
)
from src.state.schema import init_db

TODAY = "2026-11-14"
RVTY, BDX = "0000031791", "0000010795"


def _cand(**over):
    base = dict(ticker="RVTY", company_name="Revvity", event_type="investor_day",
                start_date="2025-03-20", confidence=0.9, date_grounded=True,
                sources=[CandidateSource(source_type="8K", source_url="https://sec/x.htm")])
    base.update(over)
    return CandidateEvent(**base)


# --------------------------------------------------------------- classifier mode

class _Client:
    def __init__(self):
        self.kwargs = None
        self.messages = self

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
            parsed_output=classify_mod.ExtractionResult(events=[]))


def test_past_events_mode_swaps_the_prompt_and_the_default_does_not():
    c = _Client()
    classify_mod.classify_ticker(c, "RVTY", "Revvity", [], [])
    assert c.kwargs["system"][0]["text"] is classify_mod.SYSTEM_PROMPT
    classify_mod.classify_ticker(c, "RVTY", "Revvity", [], [], past_events=True)
    assert c.kwargs["system"][0]["text"] is classify_mod.PAST_EVENTS_SYSTEM_PROMPT


def test_the_forward_prompt_still_excludes_past_events_and_the_past_prompt_does_not():
    """The weekly classifier's rule 1 is what keeps a held event off the calendar. It
    must survive the backfill being added beside it."""
    assert "Future events only." in classify_mod.SYSTEM_PROMPT
    assert "Exclude past events entirely" in classify_mod.SYSTEM_PROMPT
    past = classify_mod.PAST_EVENTS_SYSTEM_PROMPT
    assert "Exclude past events" not in past and "do NOT exclude an event for being in the past" in past


def test_past_mode_shows_the_whole_excerpt_and_the_weekly_bundle_is_cut_as_before():
    """The gate reads the whole excerpt; a date the classifier was never shown cannot
    be extracted. The weekly bundle's 1,500-character cut is unchanged."""
    hit = {"accession": "a", "filing_date": "2025-02-06", "item": "2.02", "url": "https://sec/x",
           "excerpt": "x" * 1500 + " Investor Day on March 20, 2025 " + "y" * 3000 + "END"}
    c = _Client()
    classify_mod.classify_ticker(c, "RVTY", "Revvity", [hit], [])
    weekly = c.kwargs["messages"][0]["content"]
    assert "March 20, 2025" not in weekly and "x" * 1500 in weekly
    classify_mod.classify_ticker(c, "RVTY", "Revvity", [hit], [], past_events=True)
    assert "March 20, 2025" in c.kwargs["messages"][0]["content"]
    assert "y" * 3000 + "END" in c.kwargs["messages"][0]["content"]


# --------------------------------------------------------------- upsert, historical

def test_a_held_grounded_event_is_stored_historical(tmp_path):
    conn = init_db(tmp_path / "b.db")
    _, status, is_new = upsert_event(conn, _cand(), today_iso=TODAY, historical=True)
    assert (status, is_new) == ("historical", True)
    assert conn.execute("SELECT confirmed_at FROM events").fetchone()[0] is None


@pytest.mark.parametrize("over, why", [
    (dict(date_grounded=False), "date not in the source text"),
    (dict(confidence=0.84), "under the 0.85 marquee bar"),
    (dict(sources=[CandidateSource(source_type="TAVILY_HIT", source_url="https://x/y")]),
     "no authoritative source"),
    (dict(start_date=None, date_imprecise=True, imprecise_hint="Q1 2025"), "imprecise date"),
    (dict(start_date="2026-11-14"), "dated today: not yet held"),
    (dict(start_date="2027-03-01"), "in the future"),
])
def test_historical_needs_every_gate_confirmed_needs(tmp_path, over, why):
    conn = init_db(tmp_path / "b.db")
    _, status, _ = upsert_event(conn, _cand(**over), today_iso=TODAY, historical=True)
    assert status == "discovered", why


def test_historical_mode_never_yields_confirmed_even_for_a_future_date(tmp_path):
    """The same candidate WOULD confirm in the default mode. In historical mode it
    must not: a confirmed future row is a fan-out candidate."""
    future = dict(start_date="2027-03-01")
    a, b = init_db(tmp_path / "a.db"), init_db(tmp_path / "b.db")
    assert upsert_event(a, _cand(**future), today_iso=TODAY)[1] == "confirmed"
    assert upsert_event(b, _cand(**future), today_iso=TODAY, historical=True)[1] == "discovered"


def test_default_mode_is_unchanged_a_past_event_stays_tentative(tmp_path):
    conn = init_db(tmp_path / "b.db")
    assert upsert_event(conn, _cand(), today_iso=TODAY)[1] == "tentative"


def test_a_later_grounded_filing_promotes_a_held_back_row_to_historical(tmp_path):
    conn = init_db(tmp_path / "b.db")
    eid, status, _ = upsert_event(conn, _cand(date_grounded=False), today_iso=TODAY,
                                  historical=True)
    assert status == "discovered"
    eid2, status2, is_new = upsert_event(
        conn, _cand(sources=[CandidateSource("8K", "https://sec/later.htm")]),
        today_iso=TODAY, historical=True)
    assert (eid2, status2, is_new) == (eid, "historical", False)


def test_historical_mode_leaves_a_confirmed_row_alone(tmp_path):
    conn = init_db(tmp_path / "b.db")
    upsert_event(conn, _cand(start_date="2026-12-01"), today_iso="2026-11-01")     # confirmed
    _, status, _ = upsert_event(conn, _cand(start_date="2026-12-01"),
                                today_iso="2027-01-01", historical=True)
    assert status == "confirmed"


# --------------------------------------------------------------- no fan-out, ever

def test_a_historical_row_is_invisible_to_every_output_selector(tmp_path, monkeypatch):
    """Fan-out, reminders, both digests' selectors and the published export, each asked
    with 'today' set to ON the event date -- the day a confirmed row would ping."""
    from src.cli import _fanout_candidates
    from src import digest as digest_mod

    conn = init_db(tmp_path / "b.db")
    upsert_event(conn, _cand(), today_iso=TODAY, historical=True)
    day = "2025-03-20"
    for today in (day, "2025-03-01", "2025-02-20", "2024-01-01"):
        assert _fanout_candidates(conn, today) == []
        assert due_reminders(conn, today) == []
        assert upcoming_events(conn, today, horizon_days=400) == []
    assert tentative_events(conn) == []
    assert recompute_statuses(conn, today_iso="2024-01-01") == 0
    assert conn.execute("SELECT status FROM events").fetchone()[0] == "historical"

    # The control: the same row as `confirmed` IS selected, so the asserts above are
    # measuring the status and not an empty table or a wrong date.
    conn.execute("UPDATE events SET status='confirmed'")
    assert len(_fanout_candidates(conn, day)) == 1 and len(due_reminders(conn, day)) == 1

    conn.execute("UPDATE events SET status='historical'")
    conn.commit()
    spec = importlib.util.spec_from_file_location(
        "export_upcoming_events", REPO / "scripts" / "export_upcoming_events.py")
    exp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exp)
    out = tmp_path / "up.json"
    assert exp.export(tmp_path / "b.db", out, lookback_days=1000, lookahead_days=1000) == 0
    assert "digest_mod" and digest_mod  # imported: the module loads beside a historical row


def test_the_backfill_process_loads_no_output_channel(tmp_path):
    """Asked of a fresh interpreter's loaded modules, so it holds for anything the
    backfill imports indirectly and needs no list of files kept in step."""
    code = ("import sys, src.backfill\n"
            "bad = sorted(m for m in sys.modules if m.startswith('src.') and m.split('.')[1] "
            "in ('outputs', 'cli', 'reminders', 'digest', 'health'))\n"
            "print('LOADED', bad)\n")
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "LOADED []" in r.stdout
    # ...and the check can fail: the weekly CLI does load them.
    r2 = subprocess.run([sys.executable, "-c", code.replace("src.backfill", "src.cli")],
                        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    assert "src.outputs.slack" in r2.stdout


# --------------------------------------------------------------- refusing production

def test_the_production_db_and_the_snapshot_are_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    for bad in ("data/events.db", "./data/../data/events.db", "data/ci_snapshot/events.db",
                str(tmp_path / "data" / "events.db")):
        with pytest.raises(backfill.BackfillRefused):
            backfill.assert_safe_db(bad)
    assert backfill.assert_safe_db("data/backfill_2yr.db") == (tmp_path / "data" / "backfill_2yr.db")
    assert not (tmp_path / "data").exists()          # refusing opened nothing


def test_it_does_not_run_under_github_actions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(backfill.BackfillRefused, match="GitHub Actions"):
        backfill.assert_safe_db("data/backfill_2yr.db")


def test_the_cli_refuses_before_it_sweeps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(backfill, "load_scan_universe",
                        lambda: pytest.fail("reached the universe loader"))
    assert backfill.main(["--db", "data/events.db", "--event-start", "2024-11-14",
                          "--event-end", "2026-11-14", "--filings-from", "2024-09-01"]) == 2


# --------------------------------------------------------------- windows

def test_month_windows_cover_the_range_with_no_gap_and_no_overlap():
    w = backfill.month_windows(date(2024, 9, 1), date(2026, 11, 14))
    assert len(w) == 27 and w[0] == (date(2024, 9, 1), date(2024, 9, 30))
    assert w[-1] == (date(2026, 11, 1), date(2026, 11, 14))
    assert w[3] == (date(2024, 12, 1), date(2024, 12, 31)) and w[4][0] == date(2025, 1, 1)
    for (_, e), (s, _) in zip(w, w[1:]):
        assert (s - e).days == 1
    assert backfill.month_windows(date(2025, 2, 10), date(2025, 2, 20)) == [
        (date(2025, 2, 10), date(2025, 2, 20))]
    with pytest.raises(ValueError):
        backfill.month_windows(date(2025, 2, 10), date(2025, 2, 9))


# --------------------------------------------------------------- the run, end to end

class _Resp:
    def __init__(self, payload=None, text=""):
        self._payload, self.text = payload, text

    def json(self):
        return self._payload


def _hit(cik, adsh, doc, filed):
    return {"_id": f"{adsh}:{doc}",
            "_source": {"ciks": [cik], "file_date": filed, "form": "8-K",
                        "file_type": "EX-99.1", "items": ["2.02", "9.01"]}}


# filing date -> (hit, document html). One filing per company per relevant month.
FILINGS = {
    "2025-02-06": (_hit(RVTY, "0000031791-25-000004", "rvty_q4.htm", "2025-02-06"),
                   "<p>Revvity will host its Investor Day on Thursday, March 20, 2025 in Boston.</p>"),
    "2025-05-01": (_hit(BDX, "0000010795-25-000020", "bdx_q2.htm", "2025-05-01"),
                   "<p>BD provides business update. BD will host an Investor Day on June 3 in New York.</p>"),
    "2024-09-12": (_hit(RVTY, "0000031791-24-000040", "rvty_old.htm", "2024-09-12"),
                   "<p>Revvity held its Investor Day on September 10, 2024.</p>"),
}


def _fake_get(calls, fail_months=()):
    def get(url, params=None):
        calls.append((url, params))
        if url == fts.FTS_URL:
            if params["startdt"][:7] in fail_months:
                raise RuntimeError("503 from EDGAR")
            hits = [h for filed, (h, _) in FILINGS.items()
                    if params["startdt"] <= filed <= params["enddt"]]
            return _Resp({"hits": {"total": {"value": len(hits)}, "hits": hits[params["from"]:]}})
        name = url.rsplit("/", 1)[1]
        return _Resp(text=next(html for h, html in FILINGS.values() if h["_id"].endswith(name)))
    return get


def _ev(**over):
    base = dict(event_type="investor_day", start_date="2025-03-20", end_date=None,
                multi_day=False, date_imprecise=False, imprecise_hint=None, confidence=0.92,
                source_url=None, source_type="8K", rationale="r")
    base.update(over)
    return classify_mod.ExtractedEvent(**base)


def _fake_classify(seen):
    def classify(client, ticker, name, edgar_hits, web_hits, today_iso=None, past_events=False):
        seen.append((ticker, [h["accession"] for h in edgar_hits], past_events, web_hits))
        url = edgar_hits[-1]["url"]          # the newest filing in the bundle
        if ticker == "RVTY":
            return classify_mod.ExtractionResult(events=[
                _ev(source_url=url),                                        # held, grounded
                _ev(start_date="2024-09-10", source_url=edgar_hits[0]["url"]),    # before window
                _ev(start_date="2027-02-02", source_url=url),               # after window
                _ev(event_type="conference", start_date="2025-01-14", source_url=url),
                _ev(start_date=None, date_imprecise=True, imprecise_hint="H2 2025", source_url=url),
            ])
        # BDX: "June 3" with the year only in the model's head -> not grounded
        return classify_mod.ExtractionResult(events=[_ev(start_date="2025-06-03", source_url=url)])
    return classify


def _args(tmp_path, **over):
    base = dict(db=str(tmp_path / "out" / "backfill.db"), event_start="2024-11-14",
                event_end="2026-11-14", filings_from="2024-09-01", filings_to=None,
                tickers=None, sweep_only=False, dry_run=False, workers=1,
                hits_cache=str(tmp_path / "hits.json"),
                classified_log=str(tmp_path / "classified.jsonl"),
                stats=str(tmp_path / "stats.json"))
    base.update(over)
    return argparse.Namespace(**base)


def _T(ticker, cik, name):
    return SimpleNamespace(ticker=ticker, cik=cik, company_name=name)


UNIVERSE = [_T("RVTY", "31791", "Revvity"), _T("BDX", "10795", "BD"), _T("CSL", None, "CSL Ltd")]


def _run(tmp_path, monkeypatch, calls, seen, **over):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fail = over.pop("fail_months", ())
    return backfill.run(_args(tmp_path, **over), get=_fake_get(calls, fail),
                        classify=_fake_classify(seen), client=object(), universe=UNIVERSE,
                        today=date(2026, 11, 14), log=lambda s: None)


def test_end_to_end_only_grounded_in_window_events_become_historical(tmp_path, monkeypatch):
    calls, seen = [], []
    assert _run(tmp_path, monkeypatch, calls, seen) == 0

    conn = init_db(tmp_path / "out" / "backfill.db")
    rows = {(r["ticker"], r["start_date"]): r["status"]
            for r in conn.execute("SELECT * FROM events")}
    assert rows == {("RVTY", "2025-03-20"): "historical",     # held, grounded, 8-K
                    ("BDX", "2025-06-03"): "discovered"}      # year not in the excerpt
    src = conn.execute("SELECT source_type, source_url, accession_no FROM event_sources s "
                       "JOIN events e ON e.id = s.event_id WHERE e.ticker='RVTY'").fetchone()
    assert tuple(src) == ("8K", "https://www.sec.gov/Archives/edgar/data/31791/"
                          "000003179125000004/rvty_q4.htm", "0000031791-25-000004")
    assert conn.execute("SELECT run_type, status FROM run_log").fetchall()[0][:] == ("backfill", "ok")
    # nothing in this database is, or can become, a fan-out row
    assert conn.execute("SELECT COUNT(*) FROM events WHERE status IN "
                        "('confirmed','reminded_30','reminded_7','day_of','tentative')").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE slack_posted_at IS NOT NULL OR "
                        "calendar_event_id IS NOT NULL OR ticktick_task_id IS NOT NULL").fetchone()[0] == 0

    # every classifier call was past-events mode, EDGAR only (no web hits)
    assert seen and all(past and web == [] for _, _, past, web in seen)
    assert sorted(t for t, *_ in seen) == ["BDX", "RVTY"]
    # RVTY's two filings were one call, oldest first
    assert [a for t, a, *_ in seen if t == "RVTY"] == [
        ["0000031791-24-000040", "0000031791-25-000004"]]

    s = json.loads((tmp_path / "stats.json").read_text())
    assert (s["windows"], s["windows_completed"], s["windows_failed"]) == (27, 27, {})
    assert (s["edgar_hits"], s["edgar_hits_classified"], s["events_extracted"]) == (3, 3, 6)
    assert (s["skipped_conference"], s["skipped_imprecise"], s["skipped_before_window"],
            s["skipped_after_window"], s["in_window"], s["in_window_ungrounded"]) == (1, 1, 1, 1, 2, 1)
    assert s["db_status_counts"] == {"discovered": 1, "historical": 1}
    assert s["scan_set_no_cik"] == ["CSL"]
    # one search per month, each bounded by that month
    searches = [p for u, p in calls if u == fts.FTS_URL]
    assert len(searches) == 27 and searches[0]["startdt"] == "2024-09-01"
    assert searches[-1]["enddt"] == "2026-11-14"


def test_a_rerun_repeats_neither_the_sweep_nor_the_classifier(tmp_path, monkeypatch):
    calls, seen = [], []
    _run(tmp_path, monkeypatch, calls, seen)
    calls2, seen2 = [], []
    assert _run(tmp_path, monkeypatch, calls2, seen2) == 0
    assert calls2 == [] and seen2 == []
    conn = init_db(tmp_path / "out" / "backfill.db")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2       # merged, not doubled
    assert json.loads((tmp_path / "stats.json").read_text())["batches_from_log"] == 2


def test_a_failed_month_is_named_not_cached_and_fails_the_run(tmp_path, monkeypatch):
    """A month whose search failed must never read as a month with no filings."""
    monkeypatch.setattr(fts, "_sleep", lambda s: None)
    calls, seen = [], []
    assert _run(tmp_path, monkeypatch, calls, seen, fail_months=("2025-02",)) == 1
    s = json.loads((tmp_path / "stats.json").read_text())
    assert list(s["windows_failed"]) == ["2025-02-01..2025-02-28"]
    assert (s["windows"], s["windows_completed"]) == (27, 26)
    assert "2025-02-01..2025-02-28" not in json.loads((tmp_path / "hits.json").read_text())
    conn = init_db(tmp_path / "out" / "backfill.db")
    assert conn.execute("SELECT status FROM run_log").fetchone()[0] == "partial"
    # the retry asks EDGAR for exactly the missing month and then finds the event
    calls2, seen2 = [], []
    assert _run(tmp_path, monkeypatch, calls2, seen2) == 0
    assert [p["startdt"] for u, p in calls2 if u == fts.FTS_URL] == ["2025-02-01"]
    conn = init_db(tmp_path / "out" / "backfill.db")
    assert conn.execute("SELECT status FROM events WHERE ticker='RVTY'").fetchone()[0] == "historical"


def test_a_classifier_error_is_counted_and_asked_again_next_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    seen = []
    good = _fake_classify(seen)

    def flaky(client, ticker, *a, **k):
        if ticker == "BDX":
            raise RuntimeError("overloaded")
        return good(client, ticker, *a, **k)

    kw = dict(get=_fake_get([]), client=object(), universe=UNIVERSE,
              today=date(2026, 11, 14), log=lambda s: None)
    assert backfill.run(_args(tmp_path), classify=flaky, **kw) == 1
    s = json.loads((tmp_path / "stats.json").read_text())
    assert s["classifier_errors"] == 1 and s["classifier_error_detail"][0].startswith("BDX: RuntimeError")
    seen.clear()
    assert backfill.run(_args(tmp_path), classify=good, **kw) == 0
    assert [t for t, *_ in seen] == ["BDX"]


def test_dry_run_and_sweep_only_create_no_database(tmp_path, monkeypatch):
    calls, seen = [], []
    assert _run(tmp_path, monkeypatch, calls, seen, sweep_only=True) == 0
    assert seen == [] and not (tmp_path / "out").exists()
    assert _run(tmp_path, monkeypatch, calls, seen, dry_run=True) == 0
    assert seen and not (tmp_path / "out").exists()


def test_a_url_the_model_invented_is_never_stored_as_the_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def classify(client, ticker, name, hits, web, today_iso=None, past_events=False):
        if ticker != "RVTY":
            return classify_mod.ExtractionResult(events=[])
        return classify_mod.ExtractionResult(events=[
            _ev(source_url="https://www.sec.gov/made/up.htm"),                 # date IS in the bundle
            _ev(event_type="rd_day", start_date="2025-04-04",
                source_url="https://www.sec.gov/made/up.htm")])                # date is nowhere
    assert backfill.run(_args(tmp_path), get=_fake_get([]), classify=classify, client=object(),
                        universe=UNIVERSE, today=date(2026, 11, 14), log=lambda s: None) == 0
    conn = init_db(tmp_path / "out" / "backfill.db")
    got = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT e.event_type, e.status, s.source_url FROM events e "
        "JOIN event_sources s ON s.event_id = e.id")}
    assert got["investor_day"] == ("historical", "https://www.sec.gov/Archives/edgar/data/31791/"
                                   "000003179125000004/rvty_q4.htm")
    assert got["rd_day"] == ("discovered", None)
    assert json.loads((tmp_path / "stats.json").read_text())["cited_url_not_in_bundle"] == 2


def test_the_shared_gate_matches_the_weekly_runs_gate():
    """`grounded_in_cited_source` is the rule `cli._event_grounded` applies; the backfill
    uses it so as not to import the CLI. Same inputs, same answers."""
    from src.cli import _event_grounded
    texts = {"u1": "Investor Day on March 20, 2025", "u2": "R&D Day on June 3, 2025"}
    for sd, imprecise, url in [("2025-03-20", False, "u1"), ("2025-06-03", False, "u1"),
                               ("2025-06-03", False, "elsewhere"), ("2025-07-07", False, "elsewhere"),
                               ("2025-03-20", True, "u1"), (None, False, "u1")]:
        e = SimpleNamespace(start_date=sd, date_imprecise=imprecise, source_url=url)
        assert grounded_in_cited_source(sd, imprecise, url, texts) == _event_grounded(e, texts)


# --------------------------------------------------------------- the merged export

def _load_export():
    spec = importlib.util.spec_from_file_location(
        "export_backfill", REPO / "scripts" / "export_backfill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(conn, ticker, etype, sd, status, url="https://sec/a.htm", stype="8K", conf=0.9,
         imprecise=0):
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO events (ticker, company_name, event_type, start_date, multi_day, "
        "date_imprecise, status, confidence, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
        (ticker, f"{ticker} Inc.", etype, sd, imprecise, status, conf, now, now))
    conn.execute("INSERT INTO event_sources (event_id, source_type, source_url, retrieved_at) "
                 "VALUES (?, ?, ?, ?)", (cur.lastrowid, stype, url, now))
    conn.commit()


def test_export_merges_dedupes_on_the_key_and_prefers_live(tmp_path):
    exp = _load_export()
    b, live = init_db(tmp_path / "b.db"), init_db(tmp_path / "live.db")
    _row(b, "RVTY", "investor_day", "2026-11-13", "historical", url="https://sec/back.htm")
    _row(b, "BDX", "investor_day", "2025-06-03", "historical")
    _row(b, "BDX", "rd_day", "2025-06-03", "historical")            # other type: its own row
    _row(b, "TMO", "investor_day", "2025-09-18", "discovered")      # held back: not listed
    _row(b, "ABT", "investor_day", "2024-11-13", "historical")      # the day before the window
    _row(live, "RVTY", "investor_day", "2026-11-13", "day_of", url="https://ir/live", stype="IR_PAGE")
    _row(live, "ISRG", "investor_day", "2026-08-01", "cancelled")   # retired
    _row(live, "MRNA", "rd_day", "2026-09-10", "confirmed", url="javascript:alert(1)")
    _row(live, "MRNA", "conference", "2026-01-12", "confirmed")     # not an analyst-style day
    _row(live, "DHR", "investor_day", "2026-12-10", "confirmed")    # after the window
    _row(live, "A", "investor_day", None, "tentative", imprecise=1)
    b.close(), live.close()

    items, stats = exp.merge(exp._rows(tmp_path / "b.db"), exp._rows(tmp_path / "live.db"),
                             date(2024, 11, 14), date(2026, 11, 14))
    got = [(i["ticker"], i["event_type"], i["start_date"], i["origin"]) for i in items]
    assert got == [("BDX", "investor_day", "2025-06-03", "backfill"),
                   ("BDX", "rd_day", "2025-06-03", "backfill"),
                   ("MRNA", "rd_day", "2026-09-10", "live"),
                   ("RVTY", "investor_day", "2026-11-13", "live")]
    rvty = items[-1]
    assert rvty["status"] == "day_of" and rvty["sources"] == [{"label": "IR page", "url": "https://ir/live"}]
    assert items[2]["sources"] == []                                 # non-http link withheld
    assert set(items[0]) == {"ticker", "company_name", "event_type", "start_date", "end_date",
                             "status", "confidence", "sources", "origin"}
    assert stats["clashes_live_preferred"] == [["RVTY", "investor_day", "2026-11-13"]]
    assert (stats["live_in_window"], stats["live_in_window_retired_excluded"],
            stats["backfill_historical_in_window"], stats["backfill_rows_not_listed"]) == (2, 1, 3, 1)
    assert stats["by_origin"] == {"live": 2, "backfill": 2}


def test_export_without_a_snapshot_writes_nothing(tmp_path):
    exp = _load_export()
    init_db(tmp_path / "b.db").close()
    out = tmp_path / "o.json"
    rc = exp.main(["--event-start", "2024-11-14", "--event-end", "2026-11-14",
                   "--backfill-db", str(tmp_path / "b.db"),
                   "--live-db", str(tmp_path / "nope" / "events.db"), "--out", str(out)])
    assert rc == 2 and not out.exists() and not (tmp_path / "nope").exists()


def test_backfill_only_is_explicit_and_says_so(tmp_path, capsys):
    """The live snapshot can be unavailable (an expired `gh` login, 2026-11-14). The
    backfill rows alone may be written only when asked for, and not quietly."""
    exp = _load_export()
    b = init_db(tmp_path / "b.db")
    _row(b, "BDX", "investor_day", "2025-06-03", "historical")
    b.close()
    out = tmp_path / "o.json"
    rc = exp.main(["--event-start", "2024-11-14", "--event-end", "2026-11-14", "--backfill-only",
                   "--backfill-db", str(tmp_path / "b.db"),
                   "--live-db", str(tmp_path / "nope" / "events.db"), "--out", str(out)])
    assert rc == 0
    assert [i["origin"] for i in json.loads(out.read_text())] == ["backfill"]
    printed = capsys.readouterr().out
    assert "BACKFILL ROWS ONLY" in printed and "MISSING - backfill rows only" in printed



def test_export_lists_one_row_when_live_holds_the_same_day_under_another_type(tmp_path):
    """Lilly 2026-12-07 reached the live DB as an R&D Day and other sources as an Investor
    Day. One company, one date: the live row is listed and the backfill twin is a clash."""
    exp = _load_export()
    b, live = init_db(tmp_path / "b.db"), init_db(tmp_path / "live.db")
    _row(b, "LLY", "investor_day", "2025-12-07", "historical")
    _row(b, "LLY", "investor_day", "2025-06-01", "historical")       # another date: its own row
    _row(live, "LLY", "rd_day", "2025-12-07", "confirmed")
    b.close(), live.close()
    items, stats = exp.merge(exp._rows(tmp_path / "b.db"), exp._rows(tmp_path / "live.db"),
                             date(2024, 11, 14), date(2026, 11, 14))
    assert [(i["ticker"], i["event_type"], i["start_date"], i["origin"]) for i in items] == [
        ("LLY", "investor_day", "2025-06-01", "backfill"), ("LLY", "rd_day", "2025-12-07", "live")]
    assert stats["clashes_live_preferred"] == [["LLY", "investor_day", "2025-12-07"]]
