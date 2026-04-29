# analyst-days

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
