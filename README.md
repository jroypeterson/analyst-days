# analyst-days
> Discovers upcoming Investor/Analyst/R&D/Capital Markets Days across the coverage universe (EDGAR 8-K/6-K + Tavily, Claude-classified) and fans confirmed events out to Slack, Calendar, TickTick, and a weekly email digest.

- **Status:** live (Phase 1 — core watchlist; discovery + reminders + Slack/Calendar/TickTick/email digest all wired)
- **Runtime/trigger:** Python via GitHub Actions (`monday.yml` weekly + `friday.yml` radar, 12:13 UTC) · or `python -m src.cli` on-demand
- **Reads:** Coverage Manager `exports/universe_metadata.json` (Core=Y watchlist) · SEC EDGAR 8-K/6-K · Tavily web search
- **Writes:** Slack `#analyst-days` · Google Calendar ("Other Investing") · TickTick ("Analyst Days" list) · weekly Monday/Friday email digest · `#status-reports` health heartbeat · SQLite `data/events.db`
- **Run:** `python -m src.cli --weekly` (discover → remind → digest)  ·  **Entry points:** `src/cli.py`, `src/universe.py`, `src/discovery/classify.py`

## Confirmation guardrails

A precise event date only auto-confirms (and fans out to the calendar) when it clears **three** gates, so the user never preps on a wrong day:

1. **Confidence** — per-type bar (0.85 marquee / 0.70 conference) from the classifier.
2. **Date-grounding** — the extracted date must actually appear in the *raw* source text (8-K excerpt / Tavily snippet), not the model's rationale. Catches a real announcement whose date was transcribed wrong. (`src/discovery/date_grounding.py`)
3. **Source-sensitivity** — a marquee event needs an *authoritative* source (8-K / IR page / press release); a generic web hit stays tentative until corroborated. Conferences are exempt.

Anything that fails a gate stays `tentative` (radar-only, no calendar/TickTick) until a later source corroborates it. To retire a wrong/called-off confirm without deleting it: `python -m src.cli --retire TICKER EVENT_TYPE START_DATE [--retire-as superseded] [--reason "…"]`.

## Health reporting

Both scheduled runs post a `health/v1` Block Kit heartbeat to `#status-reports` (per root `HEALTH_REPORTING.md`); verify with `python -m src.cli --health-test`. See `CLAUDE.md` → "Health reporting".

Surfaces upcoming Investor Days, Analyst Days, R&D Days, Capital Markets Days, and select industry conferences across the coverage universe. Discovery runs once a week; confirmed events fan out to Slack, Google Calendar, TickTick, and a weekly email digest.

## Pipeline

```
EDGAR 8-K (Items 7.01 / 8.01)  ─┐
                                ├─►  Claude classifier  ─►  SQLite (events.db)
Tavily web search per ticker   ─┘                          │
                                                            ├─► Slack #analyst-days  (per-confirm ping)
                                                            ├─► Google Calendar      (multi-day blocks)
                                                            ├─► TickTick "Analyst Days" list
                                                            └─► Weekly digest email + Slack (Monday)
```

## Setup

```bash
cp .env.example .env
# fill in API keys + Coverage Manager path
pip install -r requirements.txt
python -m src.cli --status
```

## Phase

Phase 1 (current): Core Watchlist (`Coverage Manager/exports/watchlist.csv` where `Core=Y`). Generalizable per-ticker discovery — Phases 2/3 swap the iterator for HC Services + MedTech, then full universe.

See `CLAUDE.md` for the full plan, schema, and operational notes.
