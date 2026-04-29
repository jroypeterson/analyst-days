"""EDGAR scanner via edgartools.

Replaces the direct-HTTP scan_8k.py. Covers:
  - 8-K  (US issuers, Items 7.01 Reg FD + 8.01 Other Events)
  - 6-K  (foreign private issuers — no item structure, scan unconditionally)

For each kept filing we walk every HTML attachment, not just the cover
document. Investor-day announcements typically live in Ex-99.* press
release exhibits which the cover-doc-only scan was missing — for one
FMS 6-K we measured 5.6 KB of cover text vs 323 KB across exhibits.

edgartools handles the SEC rate-limit ceiling, HTML/inline-XBRL parsing,
and CIK lookup via ticker. We keep the trigger regex unchanged.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Optional

import edgar
from edgar import Company

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

INTERESTING_8K_ITEMS = {"7.01", "8.01"}
RELEVANT_FORMS = ("8-K", "6-K")

_IDENTITY_SET = False


def _ensure_identity() -> None:
    """Set the SEC identity header on first call. Required by EDGAR."""
    global _IDENTITY_SET
    if _IDENTITY_SET:
        return
    identity = (
        os.environ.get("SEC_EDGAR_USER_AGENT")
        or os.environ.get("EDGAR_IDENTITY")
        or "analyst-days (jroypeterson@gmail.com)"
    )
    edgar.set_identity(identity)
    _IDENTITY_SET = True


@dataclass
class EdgarHit:
    ticker: str
    accession: str
    filing_date: str
    form: str             # "8-K" or "6-K"
    item: str             # 8-K item; empty for 6-K
    url: str              # link to the matching attachment (cover or exhibit)
    excerpt: str

    def to_dict(self) -> dict:
        return asdict(self)


def _items_set(filing) -> set[str]:
    raw = getattr(filing, "items", "") or ""
    if isinstance(raw, str):
        return {x.strip() for x in raw.split(",") if x.strip()}
    if isinstance(raw, (list, tuple)):
        return {str(x).strip() for x in raw if str(x).strip()}
    return set()


def _html_attachments(filing):
    """Yield (document_name, attachment) for HTML attachments only."""
    for a in filing.attachments:
        doc = (getattr(a, "document", "") or "").lower()
        if doc.endswith(".htm") or doc.endswith(".html"):
            yield getattr(a, "document", ""), a


def _attachment_text(att) -> str:
    try:
        return att.text() or ""
    except Exception:
        return ""


def _build_doc_url(filing, doc_name: str) -> str:
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(filing.cik)}/"
        f"{filing.accession_no.replace('-', '')}/"
        f"{doc_name}"
    )


def scan_ticker(
    ticker: str,
    cik: Optional[str] = None,  # accepted for backward compat; edgartools resolves via ticker
    lookback_days: int = 14,
    excerpt_chars: int = 600,
) -> list[EdgarHit]:
    """Scan recent 8-K + 6-K filings for the ticker; return trigger hits.

    8-Ks are filtered to Items 7.01 / 8.01. 6-Ks have no item structure
    so they're scanned unconditionally. Each kept filing has every HTML
    attachment scanned (cover doc + Ex-99 exhibits) — first hit wins
    per filing.
    """
    _ensure_identity()

    end_d = date.today()
    start_d = end_d - timedelta(days=lookback_days)
    date_range = f"{start_d.isoformat()}:{end_d.isoformat()}"

    try:
        c = Company(ticker)
    except Exception as e:
        print(f"  edgartools Company({ticker}) failed: {type(e).__name__}: {e}")
        return []

    try:
        filings = c.get_filings(form=list(RELEVANT_FORMS), filing_date=date_range)
    except Exception as e:
        print(f"  EDGAR fetch failed for {ticker}: {type(e).__name__}: {e}")
        return []

    hits: list[EdgarHit] = []
    for f in filings:
        # Form-specific item filter
        if f.form == "8-K":
            items = _items_set(f)
            relevant = items & INTERESTING_8K_ITEMS
            if not relevant:
                continue
            item_label = next(iter(sorted(relevant)), "")
        else:
            item_label = ""  # 6-K has no items

        # Walk HTML attachments. First trigger hit wins per filing —
        # one EdgarHit per filing is enough for the classifier.
        hit_recorded = False
        for doc_name, att in _html_attachments(f):
            text = _attachment_text(att)
            if not text:
                continue
            m = TRIGGER_RE.search(text)
            if not m:
                continue
            start = max(0, m.start() - excerpt_chars // 2)
            end = min(len(text), m.end() + excerpt_chars // 2)
            excerpt = text[start:end].replace("\n", " ").strip()
            url = getattr(att, "url", None) or _build_doc_url(f, doc_name)
            hits.append(EdgarHit(
                ticker=ticker,
                accession=f.accession_no,
                filing_date=str(f.filing_date),
                form=f.form,
                item=item_label,
                url=url,
                excerpt=excerpt,
            ))
            hit_recorded = True
            break

        # Fallback: cover-doc text() if no HTML attachments matched.
        # Rare — most filings have HTML attachments.
        if not hit_recorded:
            try:
                text = f.text() or ""
            except Exception:
                text = ""
            m = TRIGGER_RE.search(text)
            if m:
                start = max(0, m.start() - excerpt_chars // 2)
                end = min(len(text), m.end() + excerpt_chars // 2)
                fallback_url = (
                    str(getattr(f, "homepage_url", ""))
                    or str(getattr(f, "filing_url", ""))
                )
                hits.append(EdgarHit(
                    ticker=ticker,
                    accession=f.accession_no,
                    filing_date=str(f.filing_date),
                    form=f.form,
                    item=item_label,
                    url=fallback_url,
                    excerpt=text[start:end].replace("\n", " ").strip(),
                ))

    return hits
