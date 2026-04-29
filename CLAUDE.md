# analyst-days — Claude notes

Tracks upcoming Investor Days, Analyst Days, R&D Days, Capital Markets Days, and selected industry conferences across the coverage universe. Discovery runs weekly; output goes to Slack `#analyst-days`, Google Calendar, TickTick, and email.

## Three systems of record

- **Coverage Manager** = universe + tier assignment (which tickers to track). Consumed via local Dropbox path (`COVERAGE_MANAGER_PATH`) for dev or sparse-checkout of `jroypeterson/Coverage-Manager/exports/` in CI.
- **Google Calendar** = published event state (the same calendar `earnings_agent` writes to; titles prefixed with event type).
- **SQLite (`data/events.db`)** = workflow state + historical memory + source provenance.

## CLI modes

```
python -m src.cli --discover              # Pull EDGAR 8-Ks + Tavily; classify; insert/update events
python -m src.cli --remind                # T-30 / T-7 / day-of pings for confirmed events
python -m src.cli --digest                # Weekly Monday digest: forward 30-day + 7-day views
python -m src.cli --weekly                # discover → remind → digest in sequence (the cron entry point)
python -m src.cli --dry-run               # Preview; no Slack/Calendar/TickTick/Email writes
python -m src.cli --backlog-conferences   # Backlog task: research + add additional conferences beyond JPM
python -m src.cli --status                # Print upcoming events + DB stats
```

## Tier semantics (phase plan)

- **Phase 1** (current) — core watchlist only. ~22 tickers from `Coverage Manager/exports/watchlist.csv` where `Core=Y`.
- **Phase 2** — expand to HC Services + MedTech sectors from `universe_metadata.json`.
- **Phase 3** — full coverage universe (~1094).
- **Phase 4** — 10-year historical backfill: sweep all 8-Ks per ticker for past investor/analyst/R&D days, populate as `status=historical`.

## Event lifecycle (state machine)

```
discovered → tentative   (imprecise date, Slack/email mention only)
            → confirmed   (precise date, single authoritative source)
              → reminded_30
                → reminded_7
                  → day_of
                    → completed
```

- **Tentative** events are surfaced in Slack + email but never get Calendar / TickTick.
- **Confirmation rule**: one authoritative source counts (8-K *or* IR-page press release *or* investor relations site).
- **Confidence threshold** for auto-confirm: `>=0.80` from Claude classifier on a precise date string.
- **Reminders** fire from `confirmed` only. Each transition is one-shot — once `reminded_30` is set it never re-pings.

## Output formatting

| Channel | Title format | Behavior |
|---|---|---|
| Slack | `:calendar: New {Event Type}: {TICKER}` (bold) + date + multi-day flag + source link | Per-confirm ping; Monday digest summary |
| Calendar | `Investor Day: TICKER` / `Analyst Day: TICKER` / `R&D Day: TICKER` / `Capital Markets Day: TICKER` / `Conference: TICKER @ JPM Healthcare 2027` | Multi-day → multi-day all-day block |
| TickTick | `[Event Type] TICKER` in **"Analyst Days" list** (auto-create on first run); description includes company name + source URL + multi-day flag | Due date = event start |
| Email | Weekly Monday digest: forward 30-day + 7-day view tables | Gmail API via OAuth (reuses daily-reads token) |

## Discovery flow (per ticker)

```python
edgar_hits  = scan_8k_recent(ticker, lookback=14d, triggers=PHRASES)
tavily_hits = tavily_search(f'"{company}" "investor day" OR "analyst day" OR "R&D day" OR "capital markets day" {YEAR}')
candidates  = claude_extract(edgar_hits + tavily_hits)
    # → [{event_type, start_date, end_date, multi_day, source_url, source_type, confidence, raw_evidence}]
for c in candidates:
    if dup_in_db(c): merge_source_provenance(c)
    elif c.confidence >= 0.80 and c.date.precise:
        insert_confirmed(c)  # → slack + cal + ticktick
    elif c.date.imprecise:
        insert_tentative(c)  # → slack + email mention only
```

Conferences are a parallel iterator over `data/conferences.csv` (JPM Healthcare seeded for now).

## Cadence (two weekly fires)

| Workflow | Cron (UTC) | Local ET | Purpose |
|---|---|---|---|
| `monday.yml` | `0 12 * * 1` | Monday ~07:00 ET | discover → remind → Monday "forward 30/7" digest |
| `friday.yml` | `0 12 * * 5` | Friday ~07:00 ET | Friday "on the radar" digest (read-only — no discovery) |

No daily reminder cron. Reminders are checked once per week against current date — events crossing the T-30 or T-7 thresholds in the past 7 days are pinged on the Monday fire. Day-of pings cover anything happening this week.

**Two distinct digest shapes** in `#analyst-days`:
- **Monday Outlook** — what's *imminent*. Forward 30-day + forward 7-day tables. Drives prep.
- **Friday Radar** — *all* future events on the watchlist (confirmed + suspected, precise + imprecise). Wider inventory snapshot for the weekend reading window. Compact monospace table sorted by date.

## Required secrets (GitHub Actions)

| Secret | Source | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | New | Claude API for classify.py |
| `TAVILY_API_KEY` | Reused (daily-reads, 13F Analyzer) | Web search per ticker |
| `SLACK_WEBHOOK_ANALYST_DAYS` | New (earnings_agent Slack app, new webhook) | `#analyst-days` channel |
| `GOOGLE_CALENDAR_ID` | Reused (earnings_agent) | Same calendar as earnings |
| `GOOGLE_CREDENTIALS_JSON` | Reused (earnings_agent) | Service account JSON blob |
| `TICKTICK_ACCESS_TOKEN` | Reused (earnings_agent) | TickTick API |
| `GMAIL_OAUTH_JSON` | Reused (daily-reads) | Full token JSON content; reuses `gmail.send` scope. Locally use `GMAIL_OAUTH_JSON_PATH` instead. |
| `EMAIL_TO` | New | "to" address — `jroypeterson@gmail.com` |
| `SEC_EDGAR_USER_AGENT` or `EDGAR_IDENTITY` | Reused | Required by EDGAR |

CI also sparse-checks out `jroypeterson/Coverage-Manager/exports/` for the watchlist snapshot.

## Local `.env`

Same keys; Google creds via file path (`GOOGLE_CREDENTIALS_PATH=credentials.json`) instead of JSON blob; `COVERAGE_MANAGER_PATH=C:/Users/jroyp/Dropbox/Claude Folder/Coverage Manager`.

## Module map (target)

- `src/cli.py` — CLI entry + top-level flows (`run_discover`, `run_remind`, `run_digest`, `run_weekly`).
- `src/universe.py` — Load core watchlist from CM exports; schema version assert.
- `src/discovery/scan_8k.py` — EDGAR 8-K Item 7.01/8.01 fetch + trigger-phrase pre-filter.
- `src/discovery/scan_tavily.py` — Tavily search per ticker.
- `src/discovery/classify.py` — Claude API: extract event_type/dates/multi_day/confidence from raw hits.
- `src/discovery/conferences.py` — Parallel discovery for seeded conferences.
- `src/state/schema.py` — SQLite schema + migrations.
- `src/state/events_repo.py` — insert/update/dedupe/source-provenance.
- `src/outputs/slack.py` — `#analyst-days` webhook poster.
- `src/outputs/gcal.py` — Calendar CRUD with type-prefixed titles, multi-day support.
- `src/outputs/ticktick.py` — "Analyst Days" list management.
- `src/outputs/gmail.py` — Gmail API send via OAuth (`get_gmail_service()` reads `GMAIL_OAUTH_JSON` in CI or `GMAIL_OAUTH_JSON_PATH` locally; mirrors `daily-reads/gmail_reader.py`).
- `src/digest.py` — forward 30/7-day views, HTML + Slack blocks.
- `src/reminders.py` — T-30 / T-7 / day-of state machine.

## Pushable vs tracked-only event types

`PUSHABLE_EVENT_TYPES` in `src/state/events_repo.py` controls which event types fan out to Slack / Google Calendar / TickTick. Currently:

| Event type | Tracked in DB | Pushed to Slack | On Calendar | In digests |
|---|---|---|---|---|
| Investor Day | ✓ | ✓ | ✓ | ✓ |
| Analyst Day | ✓ | ✓ | ✓ | ✓ |
| R&D Day | ✓ | ✓ | ✓ | ✓ |
| Capital Markets Day | ✓ | ✓ | ✓ | ✓ |
| Conference | ✓ | — | — | — |

Conferences are still discovered, classified, and stored — visible via `--status` — but they don't drive prep, so they're kept off the user-facing channels. To change the policy, edit `PUSHABLE_EVENT_TYPES` then run `python -m src.cli --prune-non-pushable` to clean up calendar entries / slack-posted markers for the now-excluded types.

## Confidence thresholds (per event type)

`src/state/events_repo.py` holds the type-specific bar at which `discovered` / `tentative` events promote to `confirmed` and fan out to Slack / Calendar / TickTick.

| Event type | Threshold | Rationale |
|---|---|---|
| Investor Day | **0.85** | Headline event; high bar — wrong-date confirms drive prep on the wrong day |
| Analyst Day | **0.85** | Same |
| R&D Day | **0.85** | Same |
| Capital Markets Day | **0.85** | Same |
| Conference | **0.70** | Tavily snippets are the typical signal; failure mode is an extra calendar entry, not a wrong-date confirmation |
| (default for unknown types) | 0.80 | |

`recompute_statuses(conn)` is run at the start of `--fanout` (and at end of `--discover`). It's promotion-only — events that have already been fanned out stay confirmed even if you tighten thresholds later. Tighten the universe by deleting a row, not by demoting status.

Imprecise dates ("Q3 2026", "Fall 2026") never auto-confirm regardless of threshold — they stay `tentative` and surface in the Friday Radar (Slack/email mention only) without Calendar / TickTick fan-out until a precise corroborating source arrives.

## Backlog (not in v1)

- **Conference list expansion** beyond JPM Healthcare (ASCO, AACR, RSNA, HIMSS, ITC, HLTH, etc.).
- **10-year historical backfill** of past analyst days.
- **Webcast / replay link capture** at announcement time.
- **Slack-reply commands** (`lock`, `snooze`, `ignore`) — would require migrating from webhook to bot token + `conversations.history` scope.
- **Per-company conference slot detection** ("MRNA presenting at JPM 2027 on Day 2 at 14:30").
- **Reverse-channel to Coverage Manager**: surface tickers with no IR website populated in CM.

## Testing

`python -m pytest tests/ -q` before pushing. Tests should cover schema migrations, dedup logic, date precision parsing, and reminder state transitions.

## Git workflow

After making code changes, commit and push to GitHub (`origin master`). Follow the same "let's finish" pattern as Coverage Manager / earnings_agent / sigma-alert: save memory, update docs, run tests, commit, push.
