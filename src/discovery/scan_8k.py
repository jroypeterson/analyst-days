"""EDGAR 8-K scanner for analyst-day trigger phrases.

Pulls recent 8-K filings (Items 7.01 and 8.01) for one ticker and surfaces
those whose body text matches the trigger regex. Output is dict-shaped for
the classifier downstream — see classify.py.

EDGAR is free, requires a User-Agent with contact info, and self-throttles
at 10 req/sec — we stay under that.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Optional

import requests

EDGAR_DATA = "https://data.sec.gov"
EDGAR_WWW = "https://www.sec.gov"

# Trigger phrases — tight enough to keep false positives low, broad enough
# to catch the standard variants. Phase 1 stays conservative; we'll widen
# (e.g. "innovation day", "strategy day") only if we see false negatives.
TRIGGER_RE = re.compile(
    r"\b(?:"
    r"investor\s+day"
    r"|analyst\s+day"
    r"|investor\s+and\s+analyst\s+day"
    r"|capital\s+markets\s+day"
    r"|r\s*&\s*d\s+day|r\s+and\s+d\s+day|research\s+and\s+development\s+day"
    r")\b",
    re.IGNORECASE,
)

# Items typically used for voluntary disclosures of corporate events.
INTERESTING_ITEMS = {"7.01", "8.01"}

# Self-throttle below EDGAR's 10 req/s ceiling.
_THROTTLE_INTERVAL_SEC = 1.0 / 8.0
_LAST_REQ_AT: float = 0.0


@dataclass
class EdgarHit:
    ticker: str
    accession: str          # "0001234567-26-000123"
    filing_date: str        # ISO YYYY-MM-DD
    item: str               # "7.01" or "8.01"
    url: str                # link to primary document
    excerpt: str

    def to_dict(self) -> dict:
        return asdict(self)


def _user_agent() -> str:
    return (
        os.environ.get("SEC_EDGAR_USER_AGENT")
        or os.environ.get("EDGAR_IDENTITY")
        or "analyst-days (jroypeterson@gmail.com)"
    )


def _padded_cik(cik: str) -> str:
    return f"{int(cik):010d}"


def _accession_nodash(accession: str) -> str:
    return accession.replace("-", "")


def _throttled_get(url: str, **kwargs) -> requests.Response:
    global _LAST_REQ_AT
    elapsed = time.monotonic() - _LAST_REQ_AT
    if elapsed < _THROTTLE_INTERVAL_SEC:
        time.sleep(_THROTTLE_INTERVAL_SEC - elapsed)
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", _user_agent())
    headers.setdefault("Accept-Encoding", "gzip, deflate")
    r = requests.get(url, headers=headers, timeout=30, **kwargs)
    _LAST_REQ_AT = time.monotonic()
    r.raise_for_status()
    return r


def _fetch_recent_filings(cik: str, lookback_days: int) -> list[dict]:
    """Return 8-K filings within the lookback window, pre-filtered to interesting items."""
    url = f"{EDGAR_DATA}/submissions/CIK{_padded_cik(cik)}.json"
    data = _throttled_get(url).json()
    recent = data.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    items_list = recent.get("items", [])
    primary_docs = recent.get("primaryDocument", [])

    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    out: list[dict] = []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        if not filing_dates[i] or filing_dates[i] < cutoff:
            continue
        raw_items = items_list[i] if i < len(items_list) else ""
        items = {x.strip() for x in raw_items.split(",") if x.strip()}
        if not (items & INTERESTING_ITEMS):
            continue
        out.append({
            "accession": accessions[i],
            "filing_date": filing_dates[i],
            "items": items,
            "primary_document": primary_docs[i],
        })
    return out


def _filing_url(cik: str, accession: str, primary_document: str) -> str:
    cik_int = int(cik)
    return (
        f"{EDGAR_WWW}/Archives/edgar/data/{cik_int}/"
        f"{_accession_nodash(accession)}/{primary_document}"
    )


def _strip_html(html: str) -> str:
    # Deliberately loose — we only need the text body for regex matching.
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"&amp;", "&", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def scan_ticker(
    ticker: str,
    cik: Optional[str],
    lookback_days: int = 14,
    excerpt_chars: int = 600,
) -> list[EdgarHit]:
    """Pull recent 8-Ks for the ticker; return hits matching the trigger regex."""
    if not cik:
        return []
    try:
        filings = _fetch_recent_filings(cik, lookback_days=lookback_days)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        print(f"  EDGAR HTTP {status} for {ticker} (CIK={cik})")
        return []

    hits: list[EdgarHit] = []
    for f in filings:
        try:
            text = _strip_html(_throttled_get(
                _filing_url(cik, f["accession"], f["primary_document"])
            ).text)
        except requests.HTTPError:
            continue
        m = TRIGGER_RE.search(text)
        if not m:
            continue
        start = max(0, m.start() - excerpt_chars // 2)
        end = min(len(text), m.end() + excerpt_chars // 2)
        excerpt = text[start:end]

        item = next(iter(sorted(f["items"] & INTERESTING_ITEMS)), "")
        hits.append(EdgarHit(
            ticker=ticker,
            accession=f["accession"],
            filing_date=f["filing_date"],
            item=item,
            url=_filing_url(cik, f["accession"], f["primary_document"]),
            excerpt=excerpt,
        ))
    return hits
