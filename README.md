# analyst-days
> Discovers upcoming Investor/Analyst/R&D/Capital Markets Days across the coverage universe (EDGAR 8-K/6-K + Tavily, Claude-classified) and fans confirmed events out to Slack, Calendar, TickTick, and a weekly email digest.

- **Status:** partial (Phase 1 — core watchlist; reminders / Gmail digest expanding)
- **Runtime/trigger:** Python via GitHub Actions (`monday.yml` + `friday.yml`, 12:00 UTC) · or `python -m src.cli` on-demand
- **Reads:** Coverage Manager `exports/universe_metadata.json` (Core=Y watchlist) · SEC EDGAR 8-K/6-K · Tavily web search
- **Writes:** Slack `#analyst-days` · Google Calendar ("Other Investing") · TickTick ("Analyst Days" list) · weekly Monday/Friday email digest · SQLite `data/events.db`
- **Run:** `python -m src.cli --weekly` (discover → remind → digest)  ·  **Entry points:** `src/cli.py`, `src/universe.py`, `src/discovery/classify.py`

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
