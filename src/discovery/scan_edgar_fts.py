"""One EDGAR full-text pass for the whole scan set (replaces the per-ticker scan).

Why it changed (2026-11-13, after Revvity's Investor Day was missed):

* The per-ticker scan (`scan_edgar.scan_ticker`) read only 8-Ks tagged Item 7.01 or
  8.01. An earnings release is filed under 2.02 + 9.01, and "we will host our
  Investor Day on ..." lives in exactly that press release. Measured over the five
  Monday runs to 2026-11-09: 0, 1, 1, 0 and 1 EDGAR hits across 75 names. Full-text
  search reads every item type and every exhibit.
* It resolved the company BY TICKER and ignored the CIK it was handed. At 368 names
  that is wrong-issuer territory: `CSL` is Carlisle Companies at the SEC and CSL Ltd
  in this universe; `MED` is Medifast. Matching is now on CIK only, and a name with
  no CIK is not looked up at all (web search covers it).
* One query across all filers costs a handful of requests regardless of universe
  size, where the per-ticker scan cost a lookup and a filings pull per name.

Flow: page through EDGAR full-text search for the trigger phrases in 8-K / 6-K
filings inside the lookback window, keep documents whose filer CIK is in the scan
set, then fetch each kept document once to cut the excerpt the classifier and the
date-grounding gate read. One hit per (ticker, filing).
"""
from __future__ import annotations

import html
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Iterable, Optional

import requests

from src.discovery.scan_edgar import TRIGGER_RE, EdgarHit, EdgarScanError

FTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
FORMS = "8-K,6-K"

# The phrases EDGAR is asked for. Kept in step with `scan_edgar.TRIGGER_RE`, which
# still cuts the excerpt -- `test_every_fts_phrase_is_matched_by_the_trigger_regex`
# pins that every phrase here is one the regex accepts.
FTS_PHRASES: tuple[str, ...] = (
    "investor day", "analyst day", "investor and analyst day", "capital markets day",
    "R&D day", "R and D day", "research and development day",
    "innovation day", "pipeline day", "strategy day",
    "strategic update", "business update",
)

# SEC asks for <= 10 requests a second; stay well inside it.
MIN_INTERVAL_S = 0.15
# EDGAR full-text search will not page past 10,000 results. Two weeks of these
# phrases runs to a few hundred documents (468 measured 2026-11-13), so reaching
# this means the query changed shape and the result is no longer the whole answer.
FTS_RESULT_CEILING = 9_990
EXCERPT_CHARS = 600
MAX_EXCERPT_WINDOWS = 3

_sleep = time.sleep
_last_request = 0.0


@dataclass
class FtsScanResult:
    hits_by_ticker: dict[str, list[EdgarHit]] = field(default_factory=dict)
    documents_searched: int = 0      # every document EDGAR returned for the window
    filings_matched: int = 0         # filings by a scan-set CIK
    errors: list[str] = field(default_factory=list)   # per-filing fetch failures

    @property
    def hit_count(self) -> int:
        return sum(len(v) for v in self.hits_by_ticker.values())


def norm_cik(cik) -> Optional[str]:
    """'31791' / '0000031791' / 31791 -> '0000031791'; anything non-numeric -> None."""
    s = str(cik or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(10) if s.isdigit() and int(s) > 0 else None


def _user_agent() -> str:
    return (os.environ.get("SEC_EDGAR_USER_AGENT")
            or os.environ.get("EDGAR_IDENTITY")
            or "analyst-days (jroypeterson@gmail.com)")


# EDGAR full-text search returns sporadic 500s under load: the first 90-day dry run
# (2026-11-13) died at offset 300 of ~3,000 on one. A page failure aborts the whole
# pass by design, so a transient error has to be retried before it is believed.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_DELAYS_S = (2.0, 5.0, 15.0)


def _get(url: str, params: Optional[dict] = None) -> requests.Response:
    """GET with SEC pacing; retries a throttle / server error / dropped connection."""
    global _last_request
    for attempt in range(len(RETRY_DELAYS_S) + 1):
        gap = MIN_INTERVAL_S - (time.monotonic() - _last_request)
        if gap > 0:
            _sleep(gap)
        _last_request = time.monotonic()
        last_try = attempt == len(RETRY_DELAYS_S)
        try:
            r = requests.get(url, params=params, headers={"User-Agent": _user_agent()},
                             timeout=30)
        except (requests.ConnectionError, requests.Timeout):
            if last_try:
                raise
            _sleep(RETRY_DELAYS_S[attempt])
            continue
        if r.status_code in RETRY_STATUSES and not last_try:
            _sleep(RETRY_DELAYS_S[attempt])
            continue
        r.raise_for_status()      # a 404 is an answer, not a blip: no retry
        return r
    raise AssertionError("unreachable")


def _query() -> str:
    return " OR ".join('"%s"' % p for p in FTS_PHRASES)


def search_documents(start: date, end: date, *, get: Callable = _get) -> list[dict]:
    """Every full-text hit (one per DOCUMENT) in the window. Raises EdgarScanError on
    any page failure or a truncated result -- a partial page set must never read as
    'nothing was filed'."""
    out: list[dict] = []
    offset, total = 0, None
    while True:
        params = {"q": _query(), "forms": FORMS, "dateRange": "custom",
                  "startdt": start.isoformat(), "enddt": end.isoformat(), "from": offset}
        try:
            data = get(FTS_URL, params).json()
            hits = data["hits"]
            page = hits["hits"]
            if total is None:
                total = int((hits.get("total") or {}).get("value", 0))
        except Exception as e:                                   # noqa: BLE001
            raise EdgarScanError(
                f"EDGAR full-text search failed at offset {offset}: "
                f"{type(e).__name__}: {e}") from e
        if total > FTS_RESULT_CEILING:
            raise EdgarScanError(
                f"EDGAR full-text search matched {total} documents, past the "
                f"{FTS_RESULT_CEILING} it will page through - the result would be partial")
        out.extend(page)
        offset += len(page)
        if not page or offset >= total:
            break
    if total and len(out) < total:
        raise EdgarScanError(
            f"EDGAR full-text search returned {len(out)} of {total} documents")
    return out


_TAG_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>")
_WS_RE = re.compile(r"\s+")


def html_to_text(raw: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", raw))).strip()


def cut_excerpt(text: str, excerpt_chars: int = EXCERPT_CHARS,
                max_windows: int = MAX_EXCERPT_WINDOWS) -> str:
    """Text around the first few trigger matches, non-overlapping, joined with ' ... '.

    More than one window on purpose: a release often names the event in a headline
    ("...ahead of Investor Day") and states the date paragraphs later. The
    date-grounding gate reads only this excerpt, so the first mention alone would
    hold a correctly-dated event at tentative.
    """
    half = excerpt_chars // 2
    windows: list[tuple[int, int]] = []
    for m in TRIGGER_RE.finditer(text):
        lo, hi = max(0, m.start() - half), min(len(text), m.end() + half)
        if windows and lo <= windows[-1][1]:
            windows[-1] = (windows[-1][0], hi)
        else:
            if len(windows) == max_windows:
                break
            windows.append((lo, hi))
    return " ... ".join(text[lo:hi].strip() for lo, hi in windows)


def _doc_rank(src: dict, doc_name: str) -> tuple:
    """Press-release exhibits first: the cover page of an 8-K only LISTS
    'Press release entitled ...', and the date is in the exhibit."""
    ftype = str(src.get("file_type") or "").upper()
    return (0 if ftype.startswith("EX-99") else 1, ftype, doc_name)


def scan_universe(
    cik_to_tickers: dict[str, list[str]],
    lookback_days: int = 14,
    *,
    today: Optional[date] = None,
    get: Callable = _get,
) -> FtsScanResult:
    """Trigger hits for every scan-set filer, keyed by ticker.

    `cik_to_tickers` maps a 10-digit CIK to the scan-set ticker(s) filing under it.
    Raises EdgarScanError if the search itself fails; a single document that cannot
    be fetched is recorded in `errors` and the next document of that filing is tried.
    """
    end = today or date.today()
    start = end - timedelta(days=lookback_days)
    result = FtsScanResult()

    docs = search_documents(start, end, get=get)
    result.documents_searched = len(docs)

    # accession -> (cik, source, [document names]) for scan-set filers only
    filings: dict[tuple[str, str], tuple[dict, list[str]]] = {}
    for h in docs:
        src = h.get("_source") or {}
        hit_id = str(h.get("_id") or "")
        adsh, _, doc_name = hit_id.partition(":")
        if not adsh or not doc_name:
            continue
        for cik in src.get("ciks") or []:
            c = norm_cik(cik)
            if c in cik_to_tickers:
                filings.setdefault((c, adsh), (src, []))[1].append(
                    (_doc_rank(src, doc_name), doc_name))
    result.filings_matched = len(filings)

    for (cik, adsh), (src, ranked) in sorted(filings.items()):
        hit: Optional[EdgarHit] = None
        failures: list[str] = []
        for _, doc_name in sorted(ranked):
            url = ARCHIVE_URL.format(cik=int(cik), acc=adsh.replace("-", ""), doc=doc_name)
            try:
                text = html_to_text(get(url).text)
            except Exception as e:                               # noqa: BLE001
                failures.append(f"{doc_name}: {type(e).__name__}: {e}")
                continue
            excerpt = cut_excerpt(text)
            if not excerpt:
                continue      # EDGAR matched a phrase the regex does not (e.g. hyphenated)
            hit = EdgarHit(
                ticker="", accession=adsh, filing_date=str(src.get("file_date") or ""),
                form=str(src.get("form") or src.get("file_type") or ""),
                item=",".join(src.get("items") or []), url=url, excerpt=excerpt)
            break
        if hit is None:
            if failures:
                result.errors.append(f"{adsh} (CIK {int(cik)}): " + "; ".join(failures))
            continue
        for ticker in cik_to_tickers[cik]:
            result.hits_by_ticker.setdefault(ticker, []).append(
                EdgarHit(**{**hit.to_dict(), "ticker": ticker}))
    return result


def cik_map(universe: Iterable) -> tuple[dict[str, list[str]], list[str]]:
    """(cik -> tickers, tickers with no CIK) for a list of `universe.Ticker`."""
    by_cik: dict[str, list[str]] = {}
    no_cik: list[str] = []
    for t in universe:
        c = norm_cik(getattr(t, "cik", None))
        if c is None:
            no_cik.append(t.ticker)
        else:
            by_cik.setdefault(c, []).append(t.ticker)
    return by_cik, no_cik
