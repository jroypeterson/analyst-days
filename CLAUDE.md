# analyst-days — Claude notes

Tracks upcoming Investor Days, Analyst Days, R&D Days, Capital Markets Days, and selected industry conferences across the coverage universe. Discovery runs weekly; output goes to Slack `#analyst-days`, Google Calendar, TickTick, and email.

## Three systems of record

- **Coverage Manager** = universe + tier assignment (which tickers to track). Consumed via local Dropbox path (`COVERAGE_MANAGER_PATH`) for dev or sparse-checkout of `jroypeterson/Coverage-Manager/exports/` in CI.
- **Google Calendar** = published event state. Dedicated "Other Investing" calendar in `floridabusinessman@gmail.com` (split off the legacy shared earnings calendar 2026-05-28; titles prefixed with event type). Auth via the shared earnings-agent service account.
- **SQLite (`data/events.db`)** = workflow state + historical memory + source provenance.

## CLI modes

```
python -m src.cli --discover              # Pull EDGAR 8-Ks + Tavily; classify; insert/update events + fan-out
python -m src.cli --remind                # T-30 / T-7 / day-of pings for confirmed events
python -m src.cli --monday-digest         # Monday "forward 30/7" digest → Slack + email
python -m src.cli --friday-digest         # Friday "on the radar" digest → Slack (read-only)
python -m src.cli --weekly                # discover → remind → Monday digest in sequence (the Monday cron entry point)
python -m src.cli --status                # Print upcoming events + DB stats
python -m src.cli --slack-test            # Sanity ping to #analyst-days
python -m src.cli --gcal-test             # Verify Google Calendar auth (no writes)
python -m src.cli --gmail-test            # Verify Gmail auth (no send) — prints authorized address
python -m src.cli --ticktick-test         # Verify TickTick auth + find/create the Analyst Days list
python -m src.cli --health-test           # Post a sample health/v1 heartbeat to #status-reports (verify webhook + Block Kit)
python -m src.cli --fanout                # Re-run output fan-out without scanning
python -m src.cli --retire TICKER EVENT_TYPE START_DATE   # Retire an event off calendar/digests (deletes Calendar+TickTick, sets terminal status); --retire-as cancelled|superseded, --reason "..."
python -m src.cli --dry-run               # Preview; no DB writes / no Slack/Calendar/TickTick/Email
python -m src.cli --no-slack/--no-gcal/--no-ticktick/--no-email   # Per-channel skips (combine with any mode)
```

Manual test entry points (verify without waiting for cron): `--weekly`
locally, or the `workflow_dispatch` button on either workflow (`monday.yml`
has a `dry_run` input). `--gmail-test` / `--gcal-test` / `--ticktick-test`
verify auth in isolation.

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
- **Confirmation rule**: one authoritative source counts (8-K *or* IR-page press release *or* investor relations site), provided the date is precise AND **grounded in the raw source text** (see Date-grounding gate below).
- **Confidence threshold** for auto-confirm: per-type bar (0.85 marquee / 0.70 conference) from the Claude classifier on a precise date string — *and* the date must be grounded, else the event holds at `tentative`.
- **Reminders** fire from `confirmed` only. Each transition is one-shot — once `reminded_30` is set it never re-pings.
- **Retiring an event.** To fix a wrong-date confirm or record a called-off event, use `--retire` → a terminal `cancelled` (called off) or `superseded` (replaced by a corrected row) status. `recompute_statuses` never reconsiders these, and `export_upcoming_events.py` hides them, so the row drops off calendar/digests while preserving provenance. Prefer this over deleting the row.

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
| `monday.yml` | `13 12 * * 1` | Monday ~07:13 ET | `--weekly`: discover → remind → Monday "forward 30/7" digest (Slack + email); refresh + commit-back `exports/upcoming_events.json`; persist DB artifact |
| `friday.yml` | `13 12 * * 5` | Friday ~07:13 ET | `--friday-digest` (read-only — no discovery); reads the DB artifact the Monday run persisted |

Minute is off-`:00` deliberately (top-of-hour GH Actions crons get delayed/
skipped). The `events.db` is gitignored and persisted between runs as the
`analyst-days-db` GitHub Actions artifact (cross-run restore via the pinned
`dawidd6/action-download-artifact`); a lost artifact rebuilds from discovery —
fan-out is idempotent so the worst case is re-posting confirmed events. Both
workflows have an `if: failure()` Slack ping + an inline SMTP email backup
(for the Slack-itself-is-down case).

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
| `SLACK_WEBHOOK_STATUS_REPORTS` | Reused (shared earnings-agent #status-reports webhook) | health/v1 heartbeat → `#status-reports` (set on the repo 2026-06-30) |
| `GOOGLE_CALENDAR_ID` | Dedicated "Other Investing" calendar (floridabusinessman) | Separate from earnings since 2026-05-28 |
| `GOOGLE_CREDENTIALS_JSON` | Reused (earnings_agent) | Service account JSON blob |
| `TICKTICK_ACCESS_TOKEN` | Reused (earnings_agent) | TickTick API |
| `GMAIL_OAUTH_JSON` | Reused (daily-reads) | Full token JSON content; reuses `gmail.send` scope. Locally use `GMAIL_OAUTH_JSON_PATH` instead. |
| `EMAIL_TO` | New | "to" address — `jroypeterson@gmail.com` |
| `SEC_EDGAR_USER_AGENT` or `EDGAR_IDENTITY` | Reused | Required by EDGAR |
| `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` | Reused (earnings_agent / 13F) | Out-of-band failure email backup (inline SMTP in both workflows' `if: failure()` — fires when Slack itself is the failure point). Sends to `jroypeterson+alerts@gmail.com`. Opt-in: unset → no-op. |

CI also sparse-checks out `jroypeterson/Coverage-Manager/exports/` for the watchlist snapshot. The Coverage Manager exports schema gate is **v3** (`CM_WATCHLIST_SCHEMA_VERSION` in `src/universe.py`; bumped 2→3 on 2026-06-29 to match CM, mirroring sa-monitor 565af1c).

## Local `.env`

Same keys; Google creds via file path (`GOOGLE_CREDENTIALS_PATH=credentials.json`) instead of JSON blob; `COVERAGE_MANAGER_PATH=C:/Users/jroyp/Dropbox/Claude Folder/Coverage Manager`.

## Module map

- `src/cli.py` — CLI entry + top-level flows (`cmd_discover`, `cmd_remind`, `cmd_monday_digest`, `cmd_friday_digest`, `cmd_weekly`, the `*-test` modes).
- `src/universe.py` — Load core watchlist from CM exports; schema version assert.
- `src/discovery/scan_edgar.py` — EDGAR scanner via edgartools. Pulls 8-K (US issuers, Items 7.01/8.01) AND 6-K (foreign private issuers, no item filter) within the lookback window. For each kept filing, walks every HTML attachment (cover doc + Ex-99 exhibits — the press releases where investor-day announcements typically live) and matches against the trigger regex. First hit per filing wins.
- `src/discovery/scan_tavily.py` — Tavily search per ticker.
- `src/discovery/classify.py` — Claude API: extract event_type/dates/multi_day/confidence from raw hits.
- `src/discovery/date_grounding.py` — deterministic date-grounding gate: does the extracted ISO date appear (in any recognizable textual form) in the raw source text? Gates confirmation; see "Date-grounding gate" above.
- `src/discovery/conferences.py` — Parallel discovery for seeded conferences.
- `src/state/schema.py` — SQLite schema + migrations.
- `src/state/events_repo.py` — insert/update/dedupe/source-provenance.
- `src/outputs/slack.py` — `#analyst-days` webhook poster.
- `src/outputs/gcal.py` — Calendar CRUD with type-prefixed titles, multi-day support.
- `src/outputs/ticktick.py` — "Analyst Days" list management.
- `src/outputs/gmail.py` — Gmail API send via OAuth (`get_gmail_service()` reads `GMAIL_OAUTH_JSON` in CI or `GMAIL_OAUTH_JSON_PATH` locally; mirrors `daily-reads/gmail_reader.py`).
- `src/digest.py` — forward 30/7-day views, HTML + Slack blocks.
- `src/reminders.py` — T-30 / T-7 / day-of state machine.
- `src/health.py` — health/v1 heartbeat to `#status-reports` (Block Kit; `.health/posted` sentinel + `.health/last_run.json` fallback). See "Health reporting" below.

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

### Date-grounding gate (the wrong-date guard)

A precise date clears the confidence bar **and** must be *grounded in the raw source text* before it confirms. `src/discovery/date_grounding.py` renders the extracted ISO date into the textual forms filings actually use (e.g. `September 15, 2026`, `Sept 15`, `9/15/2026`, `15 September 2026`, `2026-09-15`) and word-boundary-matches them against the EDGAR excerpt / Tavily snippet — **not** the classifier's own `rationale` (that would be circular). Month-day-only mentions ("September 15") count only if the year also appears in the text. A precise, high-confidence event whose date isn't found in source stays `tentative` (radar-only); a later grounded source promotes it. The decision is persisted as `events.date_grounded` (schema v2) so `recompute_statuses` enforces it too. This catches the classifier transcribing a real announcement's date wrong — which `confidence` alone does not. Grounding is computed in `cli._to_candidate` from the raw hit text and shown in `--discover` output (`grounded=…`).

## Backlog (not in v1)

- **Conference list expansion** beyond JPM Healthcare (ASCO, AACR, RSNA, HIMSS, ITC, HLTH, etc.).
- **10-year historical backfill** of past analyst days.
- **Webcast / replay link capture** at announcement time.
- **Slack-reply commands** (`lock`, `snooze`, `ignore`) — would require migrating from webhook to bot token + `conversations.history` scope.
- **Per-company conference slot detection** ("MRNA presenting at JPM 2027 on Day 2 at 14:30").
- **Reverse-channel to Coverage Manager**: surface tickers with no IR website populated in CM.

## Health reporting

Both scheduled runs post a `health/v1` Block Kit heartbeat to `#status-reports`
(per root `HEALTH_REPORTING.md`), so a missing or red heartbeat is visible
fleet-wide.

- **Cadence:** Monday weekly (`--weekly`) and Friday radar (`--friday-digest`)
  — one heartbeat each, at end of run. `next_expected` points Mon→Fri and
  Fri→Mon so a reader spots a skipped run.
- **Status:** `ok` = clean; `partial` = ran + primary output usable but a
  sub-unit degraded (discovery source errors, reminder post errors, email
  digest failed, or Friday DB-not-restored); `error` = run aborted before the
  heartbeat (posted by the workflow's `if: always()` fallback step, which
  checks the `.health/posted` sentinel).
- **Counters (weekly):** tickers·hits · new·merged·fanned · reminders·errors.
- **Secret:** `SLACK_WEBHOOK_STATUS_REPORTS` (shared earnings-agent webhook).
  Verify with `--health-test`. Locally (no webhook, not CI) the post logs +
  skips; under CI an unset webhook raises.
- `src/health.py` is the poster; `.health/` (sentinel + fallback payload) is
  gitignored.

## Testing

`python -m pytest tests/ -q` before pushing. Tests should cover schema migrations, dedup logic, date precision parsing, and reminder state transitions.

## Git workflow

After making code changes, commit and push to GitHub (`origin master`). Follow the same "let's finish" pattern as Coverage Manager / earnings_agent / sigma-alert: save memory, update docs, run tests, commit, push.
