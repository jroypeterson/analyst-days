"""Turn a search model's ANSWER into a web hit the classifier and the grounding gate can trust.

Since 2026-11-13 web discovery is done by a model that searches (Codex on JP's ChatGPT
subscription, run on the Mac Studio; Perplexity as the backup) instead of by raw search
results (Tavily). A model answers "Investor Day on 2026-12-10, source <url>". That is a
CLAIM, and the pipeline's rule is that a date confirms only when it appears in the raw
source text -- never in a model's own words (see date_grounding). So nothing the model
wrote reaches the snippet:

  * the cited page is fetched here, by code;
  * the snippet is cut from that page's text, around the trigger phrases and the date;
  * if the page cannot be read, the hit carries NO date at all (not in the title either:
    the grounding gate reads `title + snippet`, and an ISO date in a title written from
    the model's answer would ground the model against itself).

The 12-case test on 2026-11-13: Codex 12 of 12, Perplexity `sonar` 10 of 10 + 2 clean
controls, Tavily 7 of 10.
"""
from __future__ import annotations

import io
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

from src.discovery.date_grounding import date_grounded_in_text
from src.discovery.scan_edgar import TRIGGER_RE
from src.discovery.scan_edgar_fts import html_to_text

EVENT_TYPES = ("investor_day", "analyst_day", "rd_day", "capital_markets_day")
MAX_BYTES = 8_000_000
MAX_PDF_PAGES = 25
WINDOW = 350
MAX_WINDOWS = 3
SNIPPET_CAP = 1400          # the classifier reads at most this much of a web snippet
UNREADABLE = "source page could not be read"


class FetchError(RuntimeError):
    """The cited page could not be fetched or read as text."""


class AnswerParseError(RuntimeError):
    """The model's reply was not the JSON object it was asked for."""


@dataclass(frozen=True)
class Answer:
    ticker: str
    event_type: str
    date: Optional[str]          # ISO, or None when no exact date is published
    source_url: str


def batch_prompt(companies: Iterable[tuple[str, str]], today: date) -> str:
    """The question, for one or many companies. `companies` = (ticker, name) pairs."""
    listing = "\n".join(f"- {t}: {name}" for t, name in companies)
    return (
        f"Today is {today.isoformat()}. Use web search. For EACH company below: has it announced "
        "that it WILL hold, or did it hold within the last 14 days, its own Investor Day, Analyst "
        "Day, R&D Day or Capital Markets Day? Do not count earnings calls, annual shareholder "
        "meetings, roadshows, or appearances at third-party / bank conferences. If there are "
        "several, report the one nearest to today. Prefer the company's own investor-relations "
        "page, its press release, or its SEC filing as the source, and give the address of a page "
        "that itself states the date - in the company's own language is fine. Do not run shell commands or "
        "read files; only search the web.\n\n"
        f"{listing}\n\n"
        "Reply with ONLY a JSON object keyed by the ticker exactly as written above. Each value is "
        '{"event_type": "investor_day" | "analyst_day" | "rd_day" | "capital_markets_day", '
        '"date": "YYYY-MM-DD", "source_url": "..."} - or null when you find no such event. '
        "Use null for date when the event is announced but no exact date is published. "
        "Include every ticker."
    )


def parse_answers(text: str, tickers: Iterable[str]) -> dict[str, Optional[Answer]]:
    """{ticker: Answer | None} for every asked ticker the reply covers.

    A ticker the reply leaves out is ABSENT from the result -- it was not searched, which
    is different from "searched, nothing found" (None). Raises AnswerParseError when the
    reply is not a JSON object at all.
    """
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise AnswerParseError("no JSON object in the reply: %r" % (text or "")[:200])
    try:
        doc = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise AnswerParseError(f"reply is not valid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise AnswerParseError("reply is not a JSON object")
    out: dict[str, Optional[Answer]] = {}
    for ticker in tickers:
        if ticker not in doc:
            continue
        v = doc[ticker]
        if v is None:
            out[ticker] = None
            continue
        if not isinstance(v, dict):
            continue                       # malformed entry: treat as not searched
        url = str(v.get("source_url") or "").strip()
        etype = str(v.get("event_type") or "").strip().lower()
        d = v.get("date")
        d = str(d).strip() if d else None
        if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            d = None
        if not _safe_url(url):
            continue                       # an event with no usable source is not evidence
        out[ticker] = Answer(ticker=ticker, event_type=etype if etype in EVENT_TYPES else "",
                             date=d, source_url=url)
    return out


def _safe_url(url: str) -> bool:
    """http(s) to a public-looking host only: this URL came from a model's output."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or "." not in host or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except ValueError:
        return True


_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _http_get(url: str) -> tuple[bytes, str]:
    """(body, content-type). Investor-relations hosts refuse plain `requests` on its TLS
    fingerprint (earnings_review measured 403s / timeouts on five of them), so curl_cffi
    impersonating Chrome is used when it is installed."""
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=30, headers=_BROWSER_HEADERS)
    except ImportError:
        import requests
        r = requests.get(url, timeout=30, headers=_BROWSER_HEADERS)
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code}")
    return r.content[:MAX_BYTES], str(r.headers.get("content-type") or "").lower()


def fetch_text(url: str, *, get: Optional[Callable] = None) -> tuple[str, str]:
    """(page title, page text) for an HTML page or a PDF. Raises FetchError."""
    get = get or _http_get      # looked up at call time: tests seal the module attribute
    if not _safe_url(url):
        raise FetchError("not a public http(s) address")
    try:
        body, ctype = get(url)
    except FetchError:
        raise
    except Exception as e:                                        # noqa: BLE001
        raise FetchError(f"{type(e).__name__}: {e}") from e
    if body[:5] == b"%PDF-" or "application/pdf" in ctype:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(body))
            text = " ".join((pg.extract_text() or "") for pg in reader.pages[:MAX_PDF_PAGES])
        except Exception as e:                                    # noqa: BLE001
            raise FetchError(f"PDF could not be read: {type(e).__name__}: {e}") from e
        text = re.sub(r"\s+", " ", text).strip()
        title = ""
    else:
        raw = body.decode("utf-8", errors="replace")
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
        title = html_to_text(m.group(1))[:200] if m else ""
        text = html_to_text(raw)
    if len(text) < 40:
        raise FetchError("page has no readable text (script-rendered or empty)")
    return title, text


def cut_snippet(text: str, iso_date: Optional[str]) -> str:
    """Raw page text around the event: windows that state the date first, then the rest.

    Only text from the page. When the page names the event in one place and the date in
    another, a window around the date is added so the grounding gate can see it.
    """
    spans: list[tuple[int, int]] = []
    for m in TRIGGER_RE.finditer(text):
        lo, hi = max(0, m.start() - WINDOW), min(len(text), m.end() + WINDOW)
        if spans and lo <= spans[-1][1]:
            spans[-1] = (spans[-1][0], hi)
        else:
            spans.append((lo, hi))
    windows = [text[lo:hi].strip() for lo, hi in spans]
    if iso_date:
        dated = [w for w in windows if date_grounded_in_text(iso_date, w)]
        if not dated and date_grounded_in_text(iso_date, text):
            step = WINDOW
            for lo in range(0, len(text), step):
                chunk = text[lo:lo + 2 * WINDOW]
                if date_grounded_in_text(iso_date, chunk):
                    dated = [chunk.strip()]
                    break
        windows = dated + [w for w in windows if w not in dated]
    out = " ... ".join(windows[:MAX_WINDOWS])
    return out[:SNIPPET_CAP]


def answer_to_hit(ticker: str, company: str, answer: Answer, *,
                  fetch: Optional[Callable] = None) -> dict:
    """A web hit shaped like the classifier's (`ticker, title, url, snippet, score`).

    `verified` says whether the cited page was read. `date_on_page` says whether the
    answer's date was found in it -- recorded for the run log, never used to confirm
    (the grounding gate re-checks the snippet itself).
    """
    fetch = fetch or fetch_text
    label = (answer.event_type or "investor event").replace("_", " ")
    try:
        title, text = fetch(answer.source_url)
    except FetchError as e:
        return {"ticker": ticker, "title": f"{company} {label} - {UNREADABLE}",
                "url": answer.source_url, "snippet": "", "score": 0.0,
                "verified": False, "date_on_page": False, "fetch_error": str(e)[:200]}
    snippet = cut_snippet(text, answer.date)
    return {"ticker": ticker, "title": (title or f"{company} {label}")[:200],
            "url": answer.source_url, "snippet": snippet, "score": 1.0,
            "verified": True,
            "date_on_page": bool(answer.date and date_grounded_in_text(answer.date, snippet))}
