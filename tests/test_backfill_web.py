"""The web-search pass of the historical backfill (2026-11-14). No network: every test
hands in a fake Codex and a fake page fetch; conftest seals the real ones."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest

from scripts import codex_web_search as codex
from src import backfill_web as bw
from src.discovery import web_answers as wa
from src.state.schema import init_db

START, END, TODAY = date(2024, 11, 14), date(2026, 11, 14), date(2026, 11, 14)
HUM_URL = "https://humana.gcs-web.com/news/humana-investor-day"
PAGE = ("Humana Investor Relations. " + "x " * 100 +
        "Humana Inc. will host its 2025 Investor Day on June 16, 2025 in New York. " + "y " * 60)


def _fetch(pages):
    def fetch(url):
        v = pages[url]
        if isinstance(v, Exception):
            raise v
        return v
    return fetch


def _conn(tmp_path):
    return init_db(str(tmp_path / "backfill.db"))


def _rows(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT ticker, event_type, start_date, status FROM events ORDER BY start_date, id")]


def _ans(d="2025-06-16", url=HUM_URL, etype="investor_day"):
    return wa.Answer("HUM", etype, d, url)


def _store(conn, answers, pages, website="https://www.humana.com"):
    return bw.store(conn, "HUM", "Humana Inc", website, answers, START, END, today=TODAY,
                    fetch=_fetch(pages), log=lambda s: None)


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parse_history_lists_events_and_separates_none_from_not_answered():
    reply = json.dumps({
        "HUM": [{"event_type": "investor_day", "date": "2025-06-16", "source_url": HUM_URL},
                {"event_type": "conference", "date": "2025-01-13", "source_url": HUM_URL},   # not marquee
                {"event_type": "investor_day", "date": None, "source_url": HUM_URL},         # undated
                {"event_type": "investor_day", "date": "2025-09-01", "source_url": "http://localhost/x"}],
        "ISRG": [], "CSL": None})
    got = bw.parse_history(reply, ["HUM", "ISRG", "CSL", "PODD"])
    assert [a.date for a in got["HUM"]] == ["2025-06-16"]
    assert got["ISRG"] == [] and got["CSL"] == [] and "PODD" not in got


def test_a_reply_that_is_not_json_raises():
    with pytest.raises(wa.AnswerParseError):
        bw.parse_history("I could not search.", ["HUM"])


# ── which sources prove an event ─────────────────────────────────────────────

@pytest.mark.parametrize("url, website, named, want", [
    ("https://www.sec.gov/Archives/edgar/data/1/x.htm", None, False, "8K"),
    ("https://investors.humana.com/events", "https://www.humana.com", False, "IR_PAGE"),
    ("https://www.businesswire.com/news/x", None, True, "PRESS_RELEASE"),
    ("https://www.businesswire.com/news/x", None, False, "TAVILY_HIT"),
    ("https://s2.q4cdn.com/123/files/deck.pdf", "https://www.humana.com", True, "IR_PAGE"),
    ("https://s2.q4cdn.com/123/files/deck.pdf", "https://www.humana.com", False, "TAVILY_HIT"),
    ("https://finance.yahoo.com/news/x", "https://www.humana.com", True, "TAVILY_HIT"),
    ("https://www.fool.com/earnings/call-transcripts/x", "https://www.humana.com", True, "TAVILY_HIT"),
    ("https://nothumana.com/x", "https://www.humana.com", True, "TAVILY_HIT"),
    ("https://ir.roche.co.jp/x", "https://www.roche.co.jp", False, "IR_PAGE"),
])
def test_source_type(url, website, named, want):
    assert bw.source_type(url, website, named) == want


def test_names_company_uses_the_distinctive_word_not_the_legal_form():
    assert bw.name_tokens("The Cooper Companies, Inc.")[0] == "cooper"
    assert bw.names_company("Quest Diagnostics to host Investor Day", "Quest Diagnostics Inc")
    assert not bw.names_company("Labcorp to host Investor Day", "Quest Diagnostics Inc")
    assert not bw.names_company("anything", "Inc")


# ── the gates, checked on the page ───────────────────────────────────────────

def test_a_checked_event_on_the_companys_own_page_is_historical(tmp_path):
    conn = _conn(tmp_path)
    c = _store(conn, [_ans()], {HUM_URL: ("Humana IR", PAGE)})
    assert _rows(conn) == [("HUM", "investor_day", "2025-06-16", "historical")]
    assert c["historical"] == 1 and c["discovered"] == 0


@pytest.mark.parametrize("answer, pages, website", [
    # the model's date is not the page's date
    (_ans("2025-06-17"), {HUM_URL: ("Humana IR", PAGE)}, "https://www.humana.com"),
    # the page cannot be read
    (_ans(), {HUM_URL: wa.FetchError("HTTP 403")}, "https://www.humana.com"),
    # a news article states the date: a lead, not proof
    (_ans(url="https://finance.yahoo.com/news/x"), {"https://finance.yahoo.com/news/x": ("Yahoo", PAGE)},
     "https://www.humana.com"),
    # the page is about another company
    (_ans(), {HUM_URL: ("Cigna IR", PAGE.replace("Humana", "Cigna"))}, "https://www.humana.com"),
    # the company's own domain, but the page it serves is about someone else
    (_ans(url="https://investors.humana.com/x"),
     {"https://investors.humana.com/x": ("Cigna IR", PAGE.replace("Humana", "Cigna"))},
     "https://www.humana.com"),
    # the date is there but nothing names an investor day
    (_ans(), {HUM_URL: ("Humana IR", "Humana Inc. annual meeting of stockholders on June 16, 2025. " * 3)},
     "https://www.humana.com"),
])
def test_an_event_that_misses_a_check_is_discovered_not_historical(tmp_path, answer, pages, website):
    conn = _conn(tmp_path)
    c = _store(conn, [answer], pages, website)
    assert [r[3] for r in _rows(conn)] == ["discovered"] and c["historical"] == 0


def test_events_outside_the_window_are_not_stored(tmp_path):
    conn = _conn(tmp_path)
    c = _store(conn, [_ans("2024-06-01"), _ans("2027-03-01")], {HUM_URL: ("Humana IR", PAGE)})
    assert _rows(conn) == [] and c["out_of_window"] == 2


def test_the_same_day_under_another_type_merges_instead_of_duplicating(tmp_path):
    """EDGAR called it an analyst day; the web answer says investor day. One day, one row."""
    conn = _conn(tmp_path)
    _store(conn, [_ans(etype="analyst_day")], {HUM_URL: ("Humana IR", PAGE)})
    c = _store(conn, [_ans(etype="investor_day")], {HUM_URL: ("Humana IR", PAGE)})
    assert _rows(conn) == [("HUM", "analyst_day", "2025-06-16", "historical")] and c["merged"] == 1


def test_a_historical_row_is_never_a_fanout_candidate(tmp_path):
    from src.cli import CONFIRMED_STATUSES
    conn = _conn(tmp_path)
    _store(conn, [_ans()], {HUM_URL: ("Humana IR", PAGE)})
    assert all(r[3] not in CONFIRMED_STATUSES for r in _rows(conn))


# ── the run: resumable, stops on quota, refuses production ───────────────────

class _T:
    def __init__(self, ticker, name, website=None):
        self.ticker, self.company_name, self.website = ticker, name, website


def _args(tmp_path, **kw):
    base = dict(db=str(tmp_path / "bf.db"), event_start="2024-11-14", event_end="2026-11-14",
                tickers=None, limit=0, dry_run=False,
                answers_log=str(tmp_path / "answers.jsonl"), stats=str(tmp_path / "stats.json"))
    base.update(kw)
    return argparse.Namespace(**base)


def test_run_stores_answers_and_a_rerun_asks_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(bw, "load_scan_universe",
                        lambda: [_T("HUM", "Humana Inc", "https://www.humana.com"), _T("ISRG", "Intuitive")])
    asked = []

    def ask(prompt, workdir=None):
        asked.append(prompt)
        return json.dumps({"HUM": [{"event_type": "investor_day", "date": "2025-06-16",
                                    "source_url": HUM_URL}], "ISRG": []}), {"input_tokens": 100}

    fetch = _fetch({HUM_URL: ("Humana IR", PAGE)})
    assert bw.run(_args(tmp_path), ask=ask, fetch=fetch, log=lambda s: None) == 0
    assert bw.run(_args(tmp_path), ask=ask, fetch=fetch, log=lambda s: None) == 0
    assert len(asked) == 1 and "between 2024-11-14 and 2026-11-14" in asked[0]
    stats = json.loads((tmp_path / "stats.json").read_text())
    assert (stats["historical"], stats["names_answered"], stats["calls"]) == (1, 2, 0)   # 2nd run: 0 calls
    import sqlite3
    conn = sqlite3.connect(tmp_path / "bf.db")
    assert _rows(conn) == [("HUM", "investor_day", "2025-06-16", "historical")]


@pytest.mark.parametrize("exc", [codex.QuotaExhausted("usage limit"),
                                 codex.SearchUnavailable("no web search ran")])
def test_a_quota_or_search_failure_stops_the_pass_at_once(tmp_path, monkeypatch, exc):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(bw, "BATCH_SIZE", 1)
    monkeypatch.setattr(bw, "load_scan_universe", lambda: [_T(t, t) for t in "ABC"])
    calls = []

    def ask(prompt, workdir=None):
        calls.append(1)
        raise exc

    assert bw.run(_args(tmp_path), ask=ask, log=lambda s: None) == 1
    stats = json.loads((tmp_path / "stats.json").read_text())
    assert len(calls) == 1 and stats["names_not_searched"] == 3 and stats["stopped"]


def test_a_ticker_the_reply_skipped_is_counted_not_searched(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(bw, "load_scan_universe", lambda: [_T("HUM", "Humana"), _T("PODD", "Insulet")])
    assert bw.run(_args(tmp_path), ask=lambda p, workdir=None: ('{"HUM": []}', {}),
                  log=lambda s: None) == 1
    assert json.loads((tmp_path / "stats.json").read_text())["names_not_searched"] == 1


def test_the_production_db_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert bw.main(["--db", "data/events.db", "--event-start", "2024-11-14",
                    "--event-end", "2026-11-14"]) == 2
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert bw.main(["--db", str(tmp_path / "x.db"), "--event-start", "2024-11-14",
                    "--event-end", "2026-11-14"]) == 2


def test_the_web_backfill_process_loads_no_output_channel(tmp_path):
    code = ("import sys, src.backfill_web\n"
            "bad = sorted(m for m in sys.modules if m.startswith('src.') and m.split('.')[1] "
            "in ('outputs', 'cli', 'reminders', 'digest', 'health'))\n"
            "print('LOADED', bad)\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                       env={**os.environ, "PYTHONPATH": str(REPO)},
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "LOADED []" in r.stdout
