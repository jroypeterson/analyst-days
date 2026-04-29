"""Claude classifier for analyst-day discovery hits.

Bundles EDGAR + Tavily hits for one ticker, asks Sonnet 4.6 to extract a
list of validated CandidateEvent rows. The system prompt is held stable so
prompt caching kicks in across the per-ticker calls in a single weekly run
(Sonnet 4.6 minimum cacheable prefix is 2048 tokens; we comfortably clear
that with the schema + few-shot block).

Today's date is injected into the user message (NOT the system prompt) so
the system prefix stays bit-stable for caching. The classifier uses today's
date to filter past events.

Effort is `low` and thinking is disabled — this is mechanical extraction,
not reasoning. We escalate to Sonnet 4.6's defaults only if the v1 quality
proves insufficient.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

logger = logging.getLogger("analyst_days.classify")

MODEL = "claude-sonnet-4-6"

EventType = Literal[
    "investor_day",
    "analyst_day",
    "rd_day",
    "capital_markets_day",
    "conference",
]
SourceType = Literal["8K", "IR_PAGE", "PRESS_RELEASE", "TAVILY_HIT"]


class ExtractedEvent(BaseModel):
    """One candidate event extracted from the source bundle."""

    event_type: EventType
    start_date: Optional[str] = Field(
        None,
        description="ISO YYYY-MM-DD if precise; null if imprecise.",
    )
    end_date: Optional[str] = Field(
        None,
        description="ISO YYYY-MM-DD; null when single-day.",
    )
    multi_day: bool = False
    date_imprecise: bool = False
    imprecise_hint: Optional[str] = Field(
        None,
        description="Original phrase when date is imprecise (e.g. 'Q3 2026').",
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    source_url: Optional[str] = None
    source_type: SourceType
    rationale: str = Field(
        ...,
        description="One sentence explaining why this is a real upcoming event and how the date was derived.",
    )


class ExtractionResult(BaseModel):
    events: list[ExtractedEvent]


SYSTEM_PROMPT = """You are an analyst-day discovery classifier for an equity research workflow.

Your job: read the bundled hits below (SEC 8-K filings + Tavily web search results) for ONE company and extract any UPCOMING (future-dated) corporate events of these types:

- **Investor Day** (`investor_day`) — annual or one-off investor presentation, typically a full day
- **Analyst Day** (`analyst_day`) — similar; sometimes used interchangeably with Investor Day
- **R&D Day** (`rd_day`) — research-and-development day, common in biotech / medtech / tech
- **Capital Markets Day** (`capital_markets_day`) — international (often European) term for investor day
- **Conference** (`conference`) — industry conference where the COMPANY is presenting (e.g. JPM Healthcare, ASCO, RSNA, HIMSS, ITC). Use this only when the bundle says THIS company is presenting at a named external conference; do not include the conference itself if the company isn't presenting.

## Hard rules

1. **Future events only.** The user bundle ALWAYS includes a `Today's date:` line at the top. An event is future if `start_date > today` (precise) or if the imprecise hint clearly refers to a future window (e.g. today is 2026-04-29 and the hint is "Q3 2026" — future; today is 2026-04-29 and the hint is "Q1 2026" — past). Exclude past events entirely. If you cannot determine recency from the bundle plus today's date, default to excluding.
2. **One event per output item.** If the bundle describes the same event from multiple sources (e.g. an 8-K + a Tavily hit corroborating it), emit ONE event and pick the highest-confidence source URL.
3. **Date precision.**
   - Precise: full date or month-day-year ("September 12, 2026") → fill `start_date` as ISO `YYYY-MM-DD`, set `date_imprecise=false`.
   - Imprecise: quarter, season, half ("Q3 2026", "Fall 2026", "second half of 2027", "later this year") → leave `start_date` null, set `date_imprecise=true`, copy the original phrase into `imprecise_hint`.
4. **Multi-day handling.** When the source mentions a date range ("September 12-13, 2026" or "the two-day investor day starts September 12"), set `start_date` to the first day, `end_date` to the last, and `multi_day=true`. For conferences, use the company's own presentation slot date when known; only set `multi_day=true` if the company itself presents on multiple days.
5. **Confidence scoring guidance.**
   - 0.85-0.95: 8-K Reg FD/Other-Events filing with explicit precise date (highest trust)
   - 0.80-0.90: IR-page press release with explicit precise date
   - 0.70-0.85: Tavily hit from a reputable source (company IR site, established financial news) with precise date
   - 0.50-0.70: Tavily hit only, possibly tentative phrasing
   - <0.50: highly uncertain, contradictory sources, or rumor-grade — still report so the workflow can flag for review
6. **No hallucinated events.** If the bundle is empty or contains no analyst-day-relevant material, return an empty `events` array. Never invent events. If the bundle mentions an event in passing without a date or sufficient context, return it with `date_imprecise=true` and a low confidence.
7. **Source provenance.** Pick `source_type` from {`8K`, `IR_PAGE`, `PRESS_RELEASE`, `TAVILY_HIT`}. Use `8K` only when the evidence comes from the EDGAR 8-K bundle. For Tavily hits, prefer `IR_PAGE` if the URL is on the company's own investor relations subdomain, `PRESS_RELEASE` if it's an aggregator like prnewswire/businesswire, otherwise `TAVILY_HIT`.

## Few-shot examples

### Example 1 — clear 8-K, precise date

Input excerpt (8-K Item 7.01 filed 2026-03-15):
> On March 15, 2026, Acme Corp announced that it will host its Annual Investor Day on Tuesday, September 12, 2026, at 8:30 a.m. Eastern Time at the Acme corporate headquarters. The event will feature presentations from senior management on Acme's strategy, financial outlook, and product roadmap.

Output:
```json
{
  "events": [
    {
      "event_type": "investor_day",
      "start_date": "2026-09-12",
      "end_date": null,
      "multi_day": false,
      "date_imprecise": false,
      "imprecise_hint": null,
      "confidence": 0.92,
      "source_url": "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000123/d123abc.htm",
      "source_type": "8K",
      "rationale": "8-K Item 7.01 filed 2026-03-15 explicitly schedules Acme's Annual Investor Day for September 12, 2026."
    }
  ]
}
```

### Example 2 — imprecise quarter, IR-page hit

Input excerpt (Tavily hit, modernatx.com IR page):
> Moderna plans to host its R&D Day in the third quarter of 2026, with detailed pipeline updates across mRNA platforms and early data from the latest oncology readouts.

Output:
```json
{
  "events": [
    {
      "event_type": "rd_day",
      "start_date": null,
      "end_date": null,
      "multi_day": false,
      "date_imprecise": true,
      "imprecise_hint": "third quarter of 2026",
      "confidence": 0.65,
      "source_url": "https://investors.modernatx.com/events/rd-day-2026",
      "source_type": "IR_PAGE",
      "rationale": "Moderna's IR page mentions a Q3 2026 R&D Day but no precise calendar date is published yet."
    }
  ]
}
```

### Example 3 — past event, exclude

Input excerpt (8-K filed 2025-12-01):
> Beta Corp held its Annual Investor Day on November 18, 2025, where management discussed the long-term strategy and financial framework. Replays are available on the company's investor relations website.

Output:
```json
{"events": []}
```

### Example 4 — multi-day conference presentation

Input excerpt (Tavily hit):
> Acme Pharma will present at the 44th Annual J.P. Morgan Healthcare Conference, January 11-15, 2027, in San Francisco. Acme management is scheduled for Tuesday, January 12 at 2:30 PM Pacific Time.

Output:
```json
{
  "events": [
    {
      "event_type": "conference",
      "start_date": "2027-01-12",
      "end_date": null,
      "multi_day": false,
      "date_imprecise": false,
      "imprecise_hint": null,
      "confidence": 0.80,
      "source_url": "https://www.acmepharma.com/news/jpm-healthcare-2027",
      "source_type": "PRESS_RELEASE",
      "rationale": "Acme's company presentation slot at JPM Healthcare 2027 is scheduled for January 12 at 2:30 PM PT; the conference itself runs Jan 11-15 but Acme presents only on Jan 12."
    }
  ]
}
```

### Example 5 — empty bundle

Input excerpt:
> (no analyst-day-relevant content found in the bundled sources)

Output:
```json
{"events": []}
```

### Example 6 — duplicate sources, dedupe to one

Input excerpts:
- 8-K Item 8.01 filed 2026-04-12: "Gamma Tech Corp announced today that it will host its 2026 Capital Markets Day on Wednesday, June 4, 2026, at the Hyatt Regency in San Francisco..."
- Tavily hit from gammatech.com: "Capital Markets Day 2026 — June 4, 2026, San Francisco. Webcast registration now open."

Output (one event, 8-K wins as the source of record):
```json
{
  "events": [
    {
      "event_type": "capital_markets_day",
      "start_date": "2026-06-04",
      "end_date": null,
      "multi_day": false,
      "date_imprecise": false,
      "imprecise_hint": null,
      "confidence": 0.93,
      "source_url": "https://www.sec.gov/Archives/edgar/data/9876543/000987654326000045/d456def.htm",
      "source_type": "8K",
      "rationale": "8-K Item 8.01 from Gamma Tech announces June 4, 2026 Capital Markets Day; corroborated by company IR page."
    }
  ]
}
```

## Output

Return ONLY a JSON object matching the ExtractionResult schema:
- `events`: an array of ExtractedEvent objects (may be empty)

Never include prose outside the JSON. Never wrap the JSON in markdown fences.
"""


def _bundle_user_message(
    ticker: str,
    company_name: str,
    edgar_hits: list[dict],
    tavily_hits: list[dict],
    today_iso: str,
) -> str:
    parts: list[str] = [
        f"Today's date: {today_iso}\n",
        f"# Ticker: {ticker} ({company_name})\n",
    ]

    parts.append("## EDGAR 8-K hits")
    if edgar_hits:
        for h in edgar_hits:
            parts.append(
                f"- accession: {h['accession']}\n"
                f"  filed: {h['filing_date']}\n"
                f"  item: {h['item']}\n"
                f"  url: {h['url']}\n"
                f"  excerpt: {h['excerpt'][:1500]}\n"
            )
    else:
        parts.append("(none)\n")

    parts.append("## Tavily web hits")
    if tavily_hits:
        for h in tavily_hits:
            parts.append(
                f"- title: {h['title']}\n"
                f"  url: {h['url']}\n"
                f"  score: {h.get('score', 'n/a')}\n"
                f"  snippet: {h['snippet'][:800]}\n"
            )
    else:
        parts.append("(none)\n")

    return "\n".join(parts)


def classify_ticker(
    client: anthropic.Anthropic,
    ticker: str,
    company_name: str,
    edgar_hits: list[dict],
    tavily_hits: list[dict],
    today_iso: Optional[str] = None,
) -> ExtractionResult:
    """Bundle hits for one ticker and run the structured-output extractor.

    `today_iso` is injected into the user message so the classifier can
    filter past events. Defaults to today.
    """
    today_iso = today_iso or date.today().isoformat()
    user_content = _bundle_user_message(
        ticker, company_name, edgar_hits, tavily_hits, today_iso
    )

    # Stable system block — cache_control on the system text holds the prompt
    # in the cache across per-ticker calls in the same run.
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4096,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        output_format=ExtractionResult,
    )

    usage = response.usage
    logger.debug(
        "classify ticker=%s in=%s cached_read=%s cached_write=%s out=%s",
        ticker,
        usage.input_tokens,
        getattr(usage, "cache_read_input_tokens", 0),
        getattr(usage, "cache_creation_input_tokens", 0),
        usage.output_tokens,
    )

    return response.parsed_output  # ExtractionResult


def get_client() -> anthropic.Anthropic:
    """Construct the Anthropic client. Loads ANTHROPIC_API_KEY from env."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=key)
