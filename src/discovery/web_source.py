"""Where a name's web hits come from this run: the Mac's Codex cache, else a live search.

Order, per name:
  1. `search_cache/web_hits.json` -- written and committed by the Mac Studio's weekly
     Codex pass (`scripts/codex_web_search.py`, JP's ChatGPT subscription). Used when the
     file is fresh and covers the name.
  2. Perplexity, when PERPLEXITY_API_KEY is set.
  3. Tavily (the pre-2026-11-13 path), when it is not.

⚠ A fallback that works hides a dead primary. Every name served by 2 or 3 is counted,
the reason the cache was not used is carried into the run summary, and the weekly
heartbeat turns `partial` and names it. A stale cache is NEVER used: last week's "no
event" for a company is not this week's.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from src.discovery import scan_perplexity
from src.discovery import scan_tavily
from src.discovery.web_answers import answer_to_hit

CACHE_PATH = Path(__file__).resolve().parents[2] / "search_cache" / "web_hits.json"
CACHE_SCHEMA = 1
# The Mac pass runs early Monday; GitHub starts the Monday job hours late (15:38-22:15 UTC
# over the eight runs to 2026-11-09). Four days also covers a mid-week manual re-run.
CACHE_MAX_AGE = timedelta(days=4)

CODEX, PERPLEXITY, TAVILY = "codex", "perplexity", "tavily"

# A few names searched live every week is the design working (a name that joined the scan
# set after the Mac's pass, a ticker one reply skipped), and an alarm that is always lit is
# one nobody reads. Past this many -- or whenever the cache itself was unusable -- the
# primary is not doing its job and the heartbeat says so.
LIVE_OK_COUNT = 5
LIVE_OK_SHARE = 0.05


def load_cache(path: Optional[Path] = None, *, now: Optional[datetime] = None
               ) -> tuple[dict[str, list[dict]], str]:
    """({ticker: [hit, ...]}, problem). An empty list means 'searched, nothing found'.

    `problem` is "" when the cache is usable, else why it is not -- and then the dict is
    empty, so every name falls through to a live search.
    """
    path = path or CACHE_PATH
    now = now or datetime.now(timezone.utc)
    if not path.exists():
        return {}, "the Mac's Codex search cache is missing"
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        if doc.get("schema_version") != CACHE_SCHEMA:
            return {}, "Codex search cache schema is %r, expected %d" % (
                doc.get("schema_version"), CACHE_SCHEMA)
        made = datetime.fromisoformat(str(doc["generated_at"]).replace("Z", "+00:00"))
        if made.tzinfo is None:
            made = made.replace(tzinfo=timezone.utc)
        results = doc["results"]
        if not isinstance(results, dict):
            raise TypeError("results is not an object")
    except Exception as e:                                        # noqa: BLE001
        return {}, f"Codex search cache unreadable: {type(e).__name__}: {str(e)[:120]}"
    age = now - made
    if age > CACHE_MAX_AGE or age < -timedelta(hours=6):
        return {}, ("Codex search cache is %.1f days old (written %s); the Mac's weekly pass "
                    "did not run or did not upload" % (age.total_seconds() / 86400, made.date()))
    out: dict[str, list[dict]] = {}
    for ticker, entry in results.items():
        hit = entry.get("hit") if isinstance(entry, dict) else None
        out[str(ticker)] = [hit] if isinstance(hit, dict) else []
    if not out:
        return {}, "Codex search cache holds no searched names"
    return out, ""


@dataclass
class WebSource:
    cache: dict[str, list[dict]] = field(default_factory=dict)
    cache_problem: str = ""
    counts: dict[str, int] = field(default_factory=lambda: {CODEX: 0, PERPLEXITY: 0, TAVILY: 0})
    unverified: int = 0

    @classmethod
    def load(cls, path: Optional[Path] = None, *, now: Optional[datetime] = None) -> "WebSource":
        cache, problem = load_cache(path, now=now)
        return cls(cache=cache, cache_problem=problem)

    def hits_for(self, ticker: str, company_name: str, *, max_results: int = 5,
                 fetch: Optional[Callable] = None) -> list[dict]:
        """Web hits for one name. Raises the live provider's error; the caller counts it."""
        if ticker in self.cache:
            self.counts[CODEX] += 1
            hits = self.cache[ticker]
        elif scan_perplexity.available():
            self.counts[PERPLEXITY] += 1
            answer = scan_perplexity.search_ticker(ticker, company_name)
            if answer is None:
                return []
            kw = {"fetch": fetch} if fetch else {}
            hits = [answer_to_hit(ticker, company_name, answer, **kw)]
        else:
            self.counts[TAVILY] += 1
            return [h.to_dict() for h in scan_tavily.search_ticker(
                ticker, company_name, max_results=max_results)]
        self.unverified += sum(1 for h in hits if h.get("verified") is False)
        return hits

    def fallback_warning(self) -> str:
        """One line for the heartbeat when the Codex cache did not serve every name."""
        live = self.counts[PERPLEXITY] + self.counts[TAVILY]
        total = live + self.counts[CODEX]
        if not live:
            return ""
        if not self.cache_problem and live <= max(LIVE_OK_COUNT, LIVE_OK_SHARE * total):
            return ""
        who = PERPLEXITY if self.counts[PERPLEXITY] else TAVILY
        why = self.cache_problem or "they were not in the Mac's Codex search cache"
        return f"web search: {live} name(s) searched live via {who} - {why}"
