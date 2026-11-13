"""The single-pass EDGAR full-text scanner (2026-11-13). No network: every test hands
`scan_universe` a fake `get`."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest

from src.discovery import scan_edgar_fts as fts
from src.discovery.scan_edgar import TRIGGER_RE, EdgarScanError

RVTY = "0000031791"
TODAY = date(2026, 11, 13)


class _Resp:
    def __init__(self, payload=None, text=""):
        self._payload, self.text = payload, text

    def json(self):
        return self._payload


def _hit(cik, adsh, doc, ftype="EX-99.1", items=("2.02", "9.01"), form="8-K"):
    return {"_id": f"{adsh}:{doc}",
            "_source": {"ciks": [cik], "file_date": "2026-11-03", "form": form,
                        "file_type": ftype, "items": list(items)}}


def _fake_get(hits, docs, page_size=100, calls=None):
    """`hits` = the full-text result set; `docs` = {document name: html or Exception}."""
    def get(url, params=None):
        if calls is not None:
            calls.append((url, params))
        if url == fts.FTS_URL:
            off = params["from"]
            return _Resp({"hits": {"total": {"value": len(hits)},
                                   "hits": hits[off:off + page_size]}})
        body = docs[url.rsplit("/", 1)[1]]
        if isinstance(body, Exception):
            raise body
        return _Resp(text=body)
    return get


RELEASE = ("<html><body><p>Revvity will be hosting its 2026 Investor Day on Friday, "
           "November 13 beginning at 9:00 a.m. ET.</p></body></html>")


def test_every_fts_phrase_is_matched_by_the_trigger_regex():
    """EDGAR is asked for these phrases and the regex cuts the excerpt: a phrase the
    regex does not accept would match at EDGAR and then yield no hit."""
    for phrase in fts.FTS_PHRASES:
        assert TRIGGER_RE.search(f"our {phrase} on May 1"), phrase


def test_an_earnings_release_under_item_2_02_is_read():
    """The Revvity filing: Items 2.02, 8.01, 9.01. The per-ticker scan kept only
    7.01 / 8.01, so a 2.02-only release (BDX, 2026-11-05) was never opened."""
    get = _fake_get([_hit(RVTY, "0000031791-26-000031", "q3.htm", items=("2.02", "9.01"))],
                    {"q3.htm": RELEASE})
    r = fts.scan_universe({RVTY: ["RVTY"]}, today=TODAY, get=get)
    (hit,) = r.hits_by_ticker["RVTY"]
    assert hit.item == "2.02,9.01" and hit.form == "8-K"
    assert "Investor Day on Friday, November 13" in hit.excerpt
    assert hit.url == "https://www.sec.gov/Archives/edgar/data/31791/000003179126000031/q3.htm"
    assert (r.documents_searched, r.filings_matched, r.hit_count) == (1, 1, 1)


def test_matching_is_by_cik_never_by_ticker():
    """`CSL` is Carlisle Companies at the SEC and CSL Ltd in the universe. A filing by
    a CIK outside the scan set must not attach to anything."""
    carlisle = "0000790051"
    get = _fake_get([_hit(carlisle, "0000790051-26-000009", "csl.htm")], {"csl.htm": RELEASE})
    r = fts.scan_universe({RVTY: ["RVTY"]}, today=TODAY, get=get)
    assert r.hits_by_ticker == {} and r.filings_matched == 0 and r.documents_searched == 1


def test_the_press_release_exhibit_is_preferred_over_the_cover_page():
    adsh = "0001628280-26-073578"
    cover = "<p>99.1 Press Release entitled Revvity ... at 2026 Investor Day</p>"
    calls = []
    get = _fake_get([_hit(RVTY, adsh, "cover.htm", ftype="8-K"),
                     _hit(RVTY, adsh, "ex991.htm", ftype="EX-99.1")],
                    {"cover.htm": cover, "ex991.htm": RELEASE}, calls=calls)
    r = fts.scan_universe({RVTY: ["RVTY"]}, today=TODAY, get=get)
    (hit,) = r.hits_by_ticker["RVTY"]            # one hit per filing, not per document
    assert hit.url.endswith("/ex991.htm")
    assert not any(u.endswith("/cover.htm") for u, _ in calls)


def test_every_page_of_results_is_read():
    hits = [_hit("%010d" % i, "%010d-26-000001" % i, "x.htm") for i in range(1, 6)]
    hits.append(_hit(RVTY, "0000031791-26-000031", "q3.htm"))      # on the last page
    calls = []
    get = _fake_get(hits, {"q3.htm": RELEASE}, page_size=2, calls=calls)
    r = fts.scan_universe({RVTY: ["RVTY"]}, today=TODAY, get=get)
    assert r.documents_searched == 6 and "RVTY" in r.hits_by_ticker
    assert [p["from"] for u, p in calls if u == fts.FTS_URL] == [0, 2, 4]


def test_a_failed_search_page_raises_instead_of_reading_as_no_filings():
    def get(url, params=None):
        raise RuntimeError("503 Service Unavailable")
    with pytest.raises(EdgarScanError, match="full-text search failed"):
        fts.scan_universe({RVTY: ["RVTY"]}, today=TODAY, get=get)


def test_a_short_page_set_raises():
    """EDGAR says 5 documents and then serves an empty page: partial, not complete."""
    def get(url, params=None):
        page = [_hit(RVTY, "0000031791-26-000031", "q3.htm")] if params["from"] == 0 else []
        return _Resp({"hits": {"total": {"value": 5}, "hits": page}})
    with pytest.raises(EdgarScanError, match="1 of 5"):
        fts.scan_universe({RVTY: ["RVTY"]}, today=TODAY, get=get)


def test_a_result_past_the_paging_ceiling_raises():
    def get(url, params=None):
        return _Resp({"hits": {"total": {"value": 20_000}, "hits": [_hit(RVTY, "a-26-1", "d.htm")]}})
    with pytest.raises(EdgarScanError, match="partial"):
        fts.scan_universe({RVTY: ["RVTY"]}, today=TODAY, get=get)


def test_a_document_that_cannot_be_fetched_is_an_error_not_a_silent_miss():
    adsh = "0000031791-26-000031"
    get = _fake_get([_hit(RVTY, adsh, "q3.htm")], {"q3.htm": RuntimeError("timeout")})
    r = fts.scan_universe({RVTY: ["RVTY"]}, today=TODAY, get=get)
    assert r.hits_by_ticker == {}
    assert len(r.errors) == 1 and adsh in r.errors[0] and "timeout" in r.errors[0]


def test_the_next_document_is_tried_when_the_first_fails():
    adsh = "0000031791-26-000031"
    get = _fake_get([_hit(RVTY, adsh, "a.htm", ftype="EX-99.1"),
                     _hit(RVTY, adsh, "b.htm", ftype="EX-99.2")],
                    {"a.htm": RuntimeError("timeout"), "b.htm": RELEASE})
    r = fts.scan_universe({RVTY: ["RVTY"]}, today=TODAY, get=get)
    assert r.hits_by_ticker["RVTY"][0].url.endswith("/b.htm") and r.errors == []


def test_two_tickers_under_one_cik_both_get_the_hit():
    get = _fake_get([_hit(RVTY, "0000031791-26-000031", "q3.htm")], {"q3.htm": RELEASE})
    r = fts.scan_universe({RVTY: ["RVTY", "RVTY.DE"]}, today=TODAY, get=get)
    assert {t: h[0].ticker for t, h in r.hits_by_ticker.items()} == {
        "RVTY": "RVTY", "RVTY.DE": "RVTY.DE"}


def test_the_window_is_the_lookback_ending_today():
    calls = []
    fts.scan_universe({RVTY: ["RVTY"]}, 14, today=TODAY, get=_fake_get([], {}, calls=calls))
    (_, params), = calls
    assert (params["startdt"], params["enddt"], params["forms"]) == (
        "2026-10-30", "2026-11-13", "8-K,6-K")


def test_excerpt_carries_a_later_mention_that_states_the_date():
    """Headline names the event; the date is paragraphs later. The date-grounding
    gate reads only the excerpt."""
    text = ("Company Announces Results Ahead of Investor Day. " + "filler " * 300
            + "The Investor Day will be held on December 10, 2026 in New York.")
    out = fts.cut_excerpt(text)
    assert "Ahead of Investor Day" in out and "December 10, 2026" in out and " ... " in out


def test_html_is_reduced_to_text():
    raw = "<style>p{}</style><p>R&amp;D&nbsp;Day</p><script>x()</script>"
    assert fts.html_to_text(raw).replace("\xa0", " ") == "R&D Day"


@pytest.mark.parametrize("raw, want", [
    ("31791", RVTY), ("0000031791", RVTY), (31791, RVTY), ("31791.0", RVTY),
    ("", None), (None, None), ("N/A", None), ("0", None),
])
def test_norm_cik(raw, want):
    assert fts.norm_cik(raw) == want


class _T:
    def __init__(self, ticker, cik, company_name="Co"):
        self.ticker, self.cik, self.company_name = ticker, cik, company_name


def test_cik_map_separates_names_with_no_cik():
    by_cik, no_cik = fts.cik_map([_T("RVTY", "31791"), _T("CSL", None), _T("4503.T", "")])
    assert by_cik == {RVTY: ["RVTY"]} and no_cik == ["CSL", "4503.T"]


# ── the seam: cmd_discover drives the scanner once and survives its outage ────

def _discover_args():
    return argparse.Namespace(tickers=None, limit=0, dry_run=True, lookback=14,
                              tavily_results=5, db="unused.db")


def test_discover_runs_one_edgar_pass_and_hands_each_ticker_its_hits(monkeypatch):
    from src import cli as cli_mod
    seen = {}

    def fake_scan(cik_to_tickers, lookback_days=14):
        seen["ciks"], seen["calls"] = cik_to_tickers, seen.get("calls", 0) + 1
        res = fts.FtsScanResult(documents_searched=3, filings_matched=1)
        res.hits_by_ticker["RVTY"] = [fts.EdgarHit(
            ticker="RVTY", accession="a", filing_date="2026-11-03", form="8-K",
            item="2.02", url="https://sec/x.htm", excerpt="Investor Day on November 13, 2026")]
        return res

    classified = []
    monkeypatch.setattr(cli_mod, "load_scan_universe",
                        lambda: [_T("RVTY", "31791", "Revvity"), _T("CSL", None, "CSL Ltd")])
    monkeypatch.setattr(cli_mod, "get_client", lambda: object())
    monkeypatch.setattr(cli_mod.scan_edgar_fts, "scan_universe", fake_scan)
    monkeypatch.setattr(cli_mod, "tavily_search", lambda *a, **k: [])
    monkeypatch.setattr(cli_mod, "classify_ticker",
                        lambda client, t, name, e, w: classified.append((t, e))
                        or cli_mod.ExtractionResult(events=[]))
    args = _discover_args()
    assert cli_mod.cmd_discover(args) == 0
    assert seen == {"ciks": {RVTY: ["RVTY"]}, "calls": 1}        # CSL: no CIK, not looked up
    assert [t for t, _ in classified] == ["RVTY"] and classified[0][1][0]["item"] == "2.02"
    s = args.health_discover
    assert (s["tickers_scanned"], s["edgar_hits_total"], s["edgar_no_cik"], s["errors"]) == (2, 1, 1, 0)


def test_an_edgar_outage_is_one_counted_error_and_web_search_still_runs(monkeypatch):
    from src import cli as cli_mod
    searched = []

    def boom(*a, **k):
        raise EdgarScanError("EDGAR full-text search failed at offset 0")

    monkeypatch.setattr(cli_mod, "load_scan_universe",
                        lambda: [_T("RVTY", "31791", "Revvity"), _T("BDX", "10795", "BD")])
    monkeypatch.setattr(cli_mod, "get_client", lambda: object())
    monkeypatch.setattr(cli_mod.scan_edgar_fts, "scan_universe", boom)
    monkeypatch.setattr(cli_mod, "tavily_search",
                        lambda ticker, *a, **k: searched.append(ticker) or [])
    args = _discover_args()
    assert cli_mod.cmd_discover(args) == 0
    assert searched == ["RVTY", "BDX"]
    assert args.health_discover["errors"] == 1 and args.health_discover["tickers_scanned"] == 2
