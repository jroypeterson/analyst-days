"""Model-answered web search (2026-11-13): Codex cache, Perplexity backup, verified hits.

No network anywhere: conftest seals the providers and the page fetch, and each test that
needs one hands in a fake.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest

from scripts import codex_web_search as cws
from src.discovery import scan_perplexity, web_answers as wa, web_source as ws
from src.discovery.date_grounding import date_grounded_in_text

TODAY = date(2026, 11, 13)
NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
PAGE = ("Becton Dickinson News. " + "filler " * 120 +
        "BD to Host Investor Day. The company will host its Investor Day on December 10, 2026 "
        "in New York. " + "more " * 80)


def _fetch(pages):
    def fetch(url):
        v = pages[url]
        if isinstance(v, Exception):
            raise v
        return v
    return fetch


# ── parsing the model's reply ────────────────────────────────────────────────

def test_parse_separates_no_event_from_not_answered():
    reply = '```json\n{"BDX": {"event_type": "investor_day", "date": "2026-12-10", ' \
            '"source_url": "https://investors.bd.com/x"}, "ISRG": null}\n```'
    got = wa.parse_answers(reply, ["BDX", "ISRG", "HCA"])
    assert got["BDX"] == wa.Answer("BDX", "investor_day", "2026-12-10", "https://investors.bd.com/x")
    assert got["ISRG"] is None                 # searched, nothing found
    assert "HCA" not in got                    # the reply left it out: NOT searched


@pytest.mark.parametrize("reply", ["I could not find anything.", "[1, 2]", '{"BDX": '])
def test_a_reply_that_is_not_the_json_object_raises(reply):
    with pytest.raises(wa.AnswerParseError):
        wa.parse_answers(reply, ["BDX"])


@pytest.mark.parametrize("url", ["", "ftp://x.com/a", "http://localhost/a", "http://10.0.0.5/a",
                                 "file:///etc/passwd", "javascript:alert(1)", "http://intranet/a"])
def test_an_event_with_no_public_source_is_not_evidence(url):
    reply = json.dumps({"BDX": {"event_type": "investor_day", "date": "2026-12-10", "source_url": url}})
    assert "BDX" not in wa.parse_answers(reply, ["BDX"])


def test_a_malformed_date_is_dropped_not_guessed():
    reply = json.dumps({"BDX": {"event_type": "investor_day", "date": "December 10",
                                "source_url": "https://investors.bd.com/x"}})
    assert wa.parse_answers(reply, ["BDX"])["BDX"].date is None


# ── the hit is built from the PAGE, never from the model's words ─────────────

def test_snippet_is_page_text_and_carries_the_date():
    a = wa.Answer("BDX", "investor_day", "2026-12-10", "https://investors.bd.com/x")
    hit = wa.answer_to_hit("BDX", "Becton Dickinson", a, fetch=_fetch({a.source_url: ("BD IR", PAGE)}))
    assert hit["verified"] and hit["date_on_page"] and hit["title"] == "BD IR"
    assert "Investor Day on December 10, 2026" in hit["snippet"]
    assert hit["snippet"] in PAGE or all(part in PAGE for part in hit["snippet"].split(" ... "))
    assert date_grounded_in_text("2026-12-10", hit["title"] + " " + hit["snippet"])


def test_a_date_the_page_does_not_state_cannot_ground():
    """The model says Dec 11; the page says Dec 10. The wrong date must be nowhere in the hit."""
    a = wa.Answer("BDX", "investor_day", "2026-12-11", "https://investors.bd.com/x")
    hit = wa.answer_to_hit("BDX", "Becton Dickinson", a, fetch=_fetch({a.source_url: ("BD IR", PAGE)}))
    assert hit["verified"] and not hit["date_on_page"]
    assert not date_grounded_in_text("2026-12-11", hit["title"] + " " + hit["snippet"])


def test_an_unreadable_page_yields_a_hit_with_no_date_anywhere():
    a = wa.Answer("BDX", "investor_day", "2026-12-10", "https://investors.bd.com/x")
    hit = wa.answer_to_hit("BDX", "Becton Dickinson", a,
                           fetch=_fetch({a.source_url: wa.FetchError("HTTP 403")}))
    assert hit["verified"] is False and hit["snippet"] == "" and hit["fetch_error"] == "HTTP 403"
    assert "2026" not in hit["title"] and "December" not in hit["title"]
    assert not date_grounded_in_text("2026-12-10", hit["title"] + " " + hit["snippet"])


def test_a_date_far_from_the_event_name_still_reaches_the_snippet():
    page = ("Investor Day 2026 registration is open. " + "boilerplate " * 200 +
            "Event date: March 25, 2027. " + "footer " * 50)
    out = wa.cut_snippet(page, "2027-03-25")
    assert "March 25, 2027" in out and "Investor Day 2026 registration" in out


def test_fetch_reads_html_and_refuses_script_only_pages():
    html = b"<html><head><title>Quest IR</title></head><body><p>Investor Day March 25, 2027 " \
           b"in New York City for analysts and investors.</p></body></html>"
    title, text = wa.fetch_text("https://ir.quest.com/x", get=lambda u: (html, "text/html"))
    assert title == "Quest IR" and "Investor Day March 25, 2027" in text
    with pytest.raises(wa.FetchError, match="no readable text"):
        wa.fetch_text("https://ir.quest.com/x", get=lambda u: (b"<html><script>x()</script></html>", "text/html"))
    with pytest.raises(wa.FetchError, match="public"):
        wa.fetch_text("http://localhost/x", get=lambda u: (html, "text/html"))


def test_the_page_fetch_is_sealed_by_default():
    with pytest.raises(wa.FetchError, match="live page fetch"):
        wa.fetch_text("https://ir.quest.com/x")


# ── the cache the Mac writes, as the GitHub job reads it ─────────────────────

def _cache(tmp_path, made=NOW - timedelta(hours=9), results=None, schema=1):
    doc = {"schema_version": schema, "provider": "codex",
           "generated_at": made.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "results": results if results is not None else {
               "BDX": {"hit": {"ticker": "BDX", "title": "BD IR", "url": "https://bd/x",
                               "snippet": "Investor Day on December 10, 2026", "score": 1.0,
                               "verified": True, "date_on_page": True}},
               "ISRG": {"hit": None}}}
    p = tmp_path / "web_hits.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_a_thursday_cache_is_still_fresh_for_a_late_monday_run(tmp_path):
    """Thu 04:30 New York (09:30 UTC) -> Monday 22:15 UTC, the latest start measured."""
    thursday = datetime(2026, 11, 19, 9, 30, tzinfo=timezone.utc)
    monday = datetime(2026, 11, 23, 22, 15, tzinfo=timezone.utc)
    assert ws.load_cache(_cache(tmp_path, made=thursday), now=monday)[1] == ""
    following = monday + timedelta(days=7)          # the pass then missed a week
    assert "days old" in ws.load_cache(_cache(tmp_path, made=thursday), now=following)[1]


def test_fresh_cache_serves_its_names_and_empty_means_searched(tmp_path):
    cache, problem = ws.load_cache(_cache(tmp_path), now=NOW)
    assert problem == "" and cache["ISRG"] == [] and cache["BDX"][0]["url"] == "https://bd/x"


@pytest.mark.parametrize("kwargs, needle", [
    ({"made": NOW - timedelta(days=7)}, "days old"),
    ({"made": NOW + timedelta(days=2)}, "days old"),
    ({"schema": 2}, "schema"),
    ({"results": {}}, "no searched names"),
])
def test_an_unusable_cache_serves_nothing_and_says_why(tmp_path, kwargs, needle):
    cache, problem = ws.load_cache(_cache(tmp_path, **kwargs), now=NOW)
    assert cache == {} and needle in problem


def test_a_missing_or_corrupt_cache_serves_nothing(tmp_path):
    assert ws.load_cache(tmp_path / "absent.json", now=NOW) == ({}, "the Mac's Codex search cache is missing")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    cache, problem = ws.load_cache(bad, now=NOW)
    assert cache == {} and "unreadable" in problem


# ── which source serves a name, and the heartbeat line when it is not Codex ──

def test_cached_names_never_reach_a_live_provider(tmp_path):
    src = ws.WebSource.load(_cache(tmp_path), now=NOW)
    assert src.hits_for("BDX", "BD")[0]["snippet"].startswith("Investor Day")
    assert src.hits_for("ISRG", "Intuitive") == []
    assert src.counts == {"codex": 2, "perplexity": 0, "tavily": 0} and src.fallback_warning() == ""


def test_a_name_outside_the_cache_goes_to_perplexity_and_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    asked = []
    monkeypatch.setattr(scan_perplexity, "search_ticker",
                        lambda t, n: asked.append(t) or wa.Answer(t, "investor_day", "2026-12-01", "https://iqv/x"))
    src = ws.WebSource.load(_cache(tmp_path), now=NOW)
    hits = src.hits_for("IQV", "IQVIA", fetch=_fetch({"https://iqv/x": ("IQVIA IR", "Investor Day on December 1, 2026 " * 3)}))
    assert asked == ["IQV"] and hits[0]["verified"] and hits[0]["date_on_page"]
    assert src.counts["perplexity"] == 1
    assert src.fallback_warning() == ""          # one new name on a healthy cache is routine


def test_many_names_outside_a_healthy_cache_is_a_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    monkeypatch.setattr(scan_perplexity, "search_ticker", lambda t, n: None)
    src = ws.WebSource.load(_cache(tmp_path), now=NOW)
    src.hits_for("BDX", "BD")
    for i in range(ws.LIVE_OK_COUNT):
        src.hits_for(f"NEW{i}", "x")
    assert src.fallback_warning() == ""
    src.hits_for("ONEMORE", "x")
    assert src.fallback_warning() == ("web search: 6 name(s) searched live via perplexity - "
                                      "they were not in the Mac's Codex search cache")


def test_a_stale_cache_sends_every_name_live_and_the_warning_names_the_cause(tmp_path, monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    monkeypatch.setattr(scan_perplexity, "search_ticker", lambda t, n: None)
    src = ws.WebSource.load(_cache(tmp_path, made=NOW - timedelta(days=8)), now=NOW)
    assert src.hits_for("BDX", "BD") == []          # last week's answer is NOT served
    assert src.counts == {"codex": 0, "perplexity": 1, "tavily": 0}
    assert "8.0 days old" in src.fallback_warning() and "did not run or did not upload" in src.fallback_warning()


def test_without_a_perplexity_key_the_old_tavily_path_serves(tmp_path, monkeypatch):
    from src.discovery import scan_tavily
    hit = scan_tavily.TavilyHit("BDX", "t", "https://x.com", "s", 0.5)
    monkeypatch.setattr(scan_tavily, "search_ticker", lambda t, n, max_results=5: [hit])
    src = ws.WebSource.load(tmp_path / "absent.json", now=NOW)
    assert src.hits_for("BDX", "BD") == [hit.to_dict()] and src.counts["tavily"] == 1
    assert "via tavily" in src.fallback_warning() and "missing" in src.fallback_warning()


def test_an_unverified_hit_is_counted(tmp_path):
    p = _cache(tmp_path, results={"BDX": {"hit": {"ticker": "BDX", "title": "x", "url": "https://bd/x",
                                                  "snippet": "", "score": 0.0, "verified": False}}})
    src = ws.WebSource.load(p, now=NOW)
    src.hits_for("BDX", "BD")
    assert src.unverified == 1


# ── Perplexity backup ────────────────────────────────────────────────────────

class _R:
    def __init__(self, content=None, status=200):
        self._c, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


def test_perplexity_answer_and_null(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    ok = _R(json.dumps({"IQV": {"event_type": "investor_day", "date": "2026-12-01", "source_url": "https://ir.iqvia.com/x"}}))
    assert scan_perplexity.search_ticker("IQV", "IQVIA", today=TODAY, post=lambda *a, **k: ok).date == "2026-12-01"
    none = _R('{"ISRG": null}')
    assert scan_perplexity.search_ticker("ISRG", "Intuitive", today=TODAY, post=lambda *a, **k: none) is None


@pytest.mark.parametrize("resp", [_R(status=429), _R("no idea"), _R('{"OTHER": null}')])
def test_perplexity_failure_raises_and_is_never_read_as_no_event(monkeypatch, resp):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    with pytest.raises(scan_perplexity.PerplexityScanError):
        scan_perplexity.search_ticker("IQV", "IQVIA", today=TODAY, post=lambda *a, **k: resp)


# ── the Mac's Codex pass ─────────────────────────────────────────────────────

class _T:
    def __init__(self, ticker, name="Co"):
        self.ticker, self.company_name = ticker, name


def _events(final=None, usage=None, error=None):
    lines = [json.dumps({"type": "thread.started"})]
    if error:
        lines.append(json.dumps({"type": "error", "message": error}))
    if final is not None:
        lines.append(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": final}}))
        lines.append(json.dumps({"type": "turn.completed", "usage": usage or {"input_tokens": 10, "output_tokens": 2}}))
    return "\n".join(lines)


def _runner(stdout, stderr="", rc=0, seen=None):
    def run(cmd, **kw):
        if seen is not None:
            seen.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)
    return run


def test_run_batch_asks_codex_with_search_on_and_the_prompt_on_stdin(monkeypatch):
    monkeypatch.setenv("CODEX_BIN", "codex")
    seen = []
    final = json.dumps({"BDX": {"event_type": "investor_day", "date": "2026-12-10", "source_url": "https://investors.bd.com/x"},
                        "ISRG": None})
    answers, usage = cws.run_batch([("BDX", "BD"), ("ISRG", "Intuitive")], TODAY,
                                   runner=_runner(_events(final), seen=seen))
    cmd, kw = seen[0]
    assert cmd[:3] == ["codex", "--search", "exec"] and cmd[-1] == "-" and "read-only" in cmd
    assert "- BDX: BD" in kw["input"] and "- ISRG: Intuitive" in kw["input"]
    assert answers["BDX"].date == "2026-12-10" and answers["ISRG"] is None and usage["input_tokens"] == 10


def test_a_usage_limit_stops_the_pass_without_a_retry(monkeypatch):
    monkeypatch.setenv("CODEX_BIN", "codex")
    with pytest.raises(cws.QuotaExhausted):
        cws.run_batch([("BDX", "BD")], TODAY, runner=_runner(
            _events(error="You've hit your usage limit. Try again at Nov 19th, 2026 3:24 AM")))
    calls = []

    def run(batch, today, workdir=None):
        calls.append([t for t, _ in batch])
        if len(calls) == 2:
            raise cws.QuotaExhausted("usage limit")
        return {t: None for t, _ in batch}, {"input_tokens": 5}

    monkeypatch.setattr(cws, "BATCH_SIZE", 2)
    found = cws.search_all([_T(t) for t in "ABCDEF"], TODAY, run=run, sleep=lambda s: None)
    assert calls == [["A", "B"], ["C", "D"]]                       # third batch never asked
    assert set(found["answers"]) == {"A", "B"} and set(found["failed"]) == {"C", "D", "E", "F"}
    assert "usage limit" in found["quota"] and all("not reached" in v for v in found["failed"].values())


def test_a_failed_batch_is_retried_once_then_reported_not_read_as_no_event(monkeypatch):
    monkeypatch.setattr(cws, "BATCH_SIZE", 2)
    attempts = []

    def run(batch, today, workdir=None):
        attempts.append(batch[0][0])
        if batch[0][0] == "A" and attempts.count("A") == 1:
            raise cws.BatchFailed("timeout")           # first batch: fails once, then works
        if batch[0][0] == "C":
            raise cws.BatchFailed("no final message")  # second batch: fails twice
        return {t: None for t, _ in batch}, {}

    found = cws.search_all([_T(t) for t in "ABCD"], TODAY, run=run, sleep=lambda s: None)
    assert attempts == ["A", "A", "C", "C"]
    assert set(found["answers"]) == {"A", "B"}
    assert set(found["failed"]) == {"C", "D"} and "failed twice" in found["failed"]["C"]


def test_a_ticker_the_reply_skipped_is_failed_not_searched(monkeypatch):
    monkeypatch.setattr(cws, "BATCH_SIZE", 3)
    found = cws.search_all([_T(t) for t in "ABC"], TODAY, sleep=lambda s: None,
                           run=lambda batch, today, workdir=None: ({"A": None, "B": None}, {}))
    assert set(found["answers"]) == {"A", "B"} and found["failed"] == {"C": "the reply did not cover this ticker"}


def test_build_cache_round_trips_through_the_reader(tmp_path):
    found = {"answers": {"BDX": wa.Answer("BDX", "investor_day", "2026-12-10", "https://bd/x"), "ISRG": None},
             "failed": {"HCA": "batch failed twice: x"}, "usage": {"calls": 1}, "quota": ""}
    cache = cws.build_cache([_T("BDX", "BD"), _T("ISRG"), _T("HCA")], found, now=NOW,
                            fetch=_fetch({"https://bd/x": ("BD IR", PAGE)}))
    p = tmp_path / "web_hits.json"
    p.write_text(json.dumps(cache), encoding="utf-8")
    served, problem = ws.load_cache(p, now=NOW)
    assert problem == "" and set(served) == {"BDX", "ISRG"}        # HCA absent -> searched live
    assert served["BDX"][0]["date_on_page"] and cache["searched_count"] == 2


def test_upload_sends_the_current_sha_and_fails_loudly(tmp_path):
    p = tmp_path / "web_hits.json"
    p.write_text("{}", encoding="utf-8")
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw.get("input")))
        return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n" if "--jq" in cmd else "{}", stderr="")

    cws.upload(p, runner=run)
    body = json.loads(calls[1][1])
    assert calls[1][0][:4] == ["gh", "api", "-X", "PUT"] and body["sha"] == "abc123" and body["branch"] == "master"
    with pytest.raises(RuntimeError, match="upload failed"):
        cws.upload(p, runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 409"))


# ── the seam: discovery uses the cache and the heartbeat says when it did not ─

def test_discover_reports_which_source_served_and_warns_on_fallback(tmp_path, monkeypatch):
    from src import cli as cli_mod
    from src.discovery import scan_edgar_fts as fts
    monkeypatch.setattr(ws, "CACHE_PATH", _cache(tmp_path, made=datetime.now(timezone.utc)))
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    monkeypatch.setattr(scan_perplexity, "search_ticker", lambda t, n: None)
    monkeypatch.setattr(cli_mod, "load_scan_universe",
                        lambda: [type("T", (), {"ticker": t, "company_name": t, "cik": None})()
                                 for t in ("BDX", "ISRG", "NEWNAME")])
    monkeypatch.setattr(cli_mod, "get_client", lambda: object())
    monkeypatch.setattr(cli_mod.scan_edgar_fts, "scan_universe", lambda *a, **k: fts.FtsScanResult())
    classified = []
    monkeypatch.setattr(cli_mod, "classify_ticker",
                        lambda client, t, name, e, w: classified.append((t, w))
                        or cli_mod.ExtractionResult(events=[]))
    args = argparse.Namespace(tickers=None, limit=0, dry_run=True, lookback=14, tavily_results=5, db="x.db")
    assert cli_mod.cmd_discover(args) == 0
    s = args.health_discover
    assert (s["web_codex"], s["web_perplexity"], s["web_tavily"], s["errors"]) == (2, 1, 0, 0)
    assert s["web_fallback_warning"] == ""       # one name live on a fresh cache: not an alarm
    # only BDX had a hit, so only BDX cost a classifier call, and it got the cached page text
    assert [t for t, _ in classified] == ["BDX"] and "December 10, 2026" in classified[0][1][0]["snippet"]


def test_the_heartbeat_goes_partial_and_names_the_fallback(monkeypatch):
    """A backup that works hides a dead primary: the Mac's pass not running must show."""
    from src import cli as cli_mod
    captured = {}
    monkeypatch.setattr(cli_mod.health_mod, "post_health", lambda hb: captured.setdefault("hb", hb))
    line = "web search: 368 name(s) searched live via perplexity - the Mac's Codex search cache is missing"

    def run(discover):
        captured.clear()
        args = argparse.Namespace(health_discover=discover, health_remind={}, health_digest={})
        cli_mod._post_weekly_health(args, datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc))
        return captured["hb"]

    hb = run({"tickers_scanned": 368, "web_perplexity": 368, "web_fallback_warning": line})
    assert hb.status == "partial" and line in hb.warnings
    hb = run({"tickers_scanned": 368, "web_codex": 368, "web_fallback_warning": ""})
    assert hb.status == "ok" and not any("web search" in w for w in hb.warnings)


@pytest.mark.parametrize("failed, quota, want", [
    (0, "", 0), (18, "", 0), (19, "", 1), (1, "ChatGPT subscription usage limit", 1)])
def test_exit_code_alarms_on_quota_or_a_real_shortfall_not_on_a_stray_ticker(failed, quota, want):
    cache = {"failed": {f"T{i}": "x" for i in range(failed)}, "quota_stop": quota, "scan_set_count": 368}
    assert cws.exit_code(cache) == want
