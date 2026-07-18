"""Tavily web search for analyst-day discovery.

Per-ticker query against Tavily's REST API. Returns dict-shaped hits that
the classifier consumes alongside EDGAR hits.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import date

import requests

TAVILY_URL = "https://api.tavily.com/search"


class TavilyScanError(RuntimeError):
    """A Tavily failure that presents as HTTP 200 but carries no results.

    Tavily can return `200 {"error": "rate_limited", ...}` (or any body without
    a `results` key). Parsing that as an empty hit list is indistinguishable
    from a genuine "nothing found" — a silent zero-result that keeps health
    green. Raise so the caller counts an error and posts a partial heartbeat.
    """


@dataclass
class TavilyHit:
    ticker: str
    title: str
    url: str
    snippet: str
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


def search_ticker(
    ticker: str,
    company_name: str,
    max_results: int = 5,
    search_depth: str = "basic",
) -> list[TavilyHit]:
    """Search Tavily for upcoming analyst-day events tied to this company.

    `search_depth='basic'` is the cheaper tier; switch to 'advanced' if recall
    becomes the bottleneck once we have signal volume.
    """
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY not set")

    year = date.today().year
    next_year = year + 1
    query = (
        f'"{company_name}" ("investor day" OR "analyst day" OR "R&D day" '
        f'OR "capital markets day") {year} {next_year}'
    )
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "include_raw_content": False,
        "include_answer": False,
    }
    r = requests.post(TAVILY_URL, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()

    # A well-formed Tavily response always carries a `results` list (possibly
    # empty). An `error` field, or a missing `results` key, is a soft failure
    # dressed as 200 — surface it rather than reporting a clean zero-result.
    if not isinstance(data, dict) or "results" not in data or data.get("error"):
        err = ""
        if isinstance(data, dict):
            err = data.get("error") or data.get("message") or ""
        raise TavilyScanError(
            f"Tavily returned no results field for {ticker}"
            + (f": {err}" if err else "")
        )

    out: list[TavilyHit] = []
    for item in data.get("results", []):
        out.append(TavilyHit(
            ticker=ticker,
            title=(item.get("title") or "")[:200],
            url=item.get("url") or "",
            snippet=(item.get("content") or "")[:800],
            score=float(item.get("score") or 0.0),
        ))
    return out
