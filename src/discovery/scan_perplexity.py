"""Perplexity `sonar` -- the BACKUP web search, one company per call.

Used only for names the Mac's Codex pass did not cover (the cache is missing or stale, a
batch failed, the subscription quota ran out, or the name joined the scan set after the
pass). About USD 0.005 a call (measured 2026-11-13: USD 0.058 for 12).
"""
from __future__ import annotations

import os
from datetime import date
from typing import Optional

import requests

from src.discovery.web_answers import Answer, AnswerParseError, batch_prompt, parse_answers

URL = "https://api.perplexity.ai/chat/completions"
MODEL = "sonar"


class PerplexityScanError(RuntimeError):
    """Perplexity could not answer for this name. Raised, never read as 'no event'."""


def available() -> bool:
    return bool(os.environ.get("PERPLEXITY_API_KEY", "").strip())


_post = requests.post           # tests seal this attribute


def search_ticker(ticker: str, company_name: str, *, today: Optional[date] = None,
                  post=None) -> Optional[Answer]:
    """The model's answer for one company: an Answer, or None for 'no such event'."""
    post = post or _post
    key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not key:
        raise PerplexityScanError("PERPLEXITY_API_KEY not set")
    try:
        r = post(URL, headers={"Authorization": f"Bearer {key}"}, timeout=120, json={
            "model": MODEL, "temperature": 0,
            "messages": [{"role": "user",
                          "content": batch_prompt([(ticker, company_name)], today or date.today())}],
            "web_search_options": {"search_context_size": "low"}})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    except Exception as e:                                        # noqa: BLE001
        raise PerplexityScanError(f"{ticker}: {type(e).__name__}: {str(e)[:200]}") from e
    try:
        answers = parse_answers(text, [ticker])
    except AnswerParseError as e:
        raise PerplexityScanError(f"{ticker}: {e}") from e
    if ticker not in answers:
        raise PerplexityScanError(f"{ticker}: the reply did not cover this ticker")
    return answers[ticker]
