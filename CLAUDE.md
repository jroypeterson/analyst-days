# analyst-days — Claude notes

Tracks upcoming Investor Days, Analyst Days, R&D Days, Capital Markets Days, and selected industry conferences across the coverage universe. Discovery runs weekly; output goes to Slack `#analyst-days`, Google Calendar, TickTick, and email.

## Three systems of record

- **Coverage Manager** = universe + tier assignment (which tickers to track). Consumed via local Dropbox path (`COVERAGE_MANAGER_PATH`) for dev or sparse-checkout of `jroypeterson/Coverage-Manager/exports/` in CI.
- **Google Calendar** = published event state. Dedicated "Other Investing" calendar in `floridabusinessman@gmail.com` (split off the legacy shared earnings calendar 2026-05-28; titles prefixed with event type). Auth via the shared earnings-agent service account.
- **SQLite (`data/events.db`)** = workflow state + historical memory + source provenance.

## The conference calendar is CONFERENCE-anchored; `events` is COMPANY-anchored

Two different questions, and only one of them the discovery pipeline can answer.

`events.event_type='conference'` means **a covered company is presenting somewhere** —
the classifier is explicit: *"use this only when the bundle says THIS company is
presenting at a named external conference; do not include the conference itself if the
company isn't presenting."* Conferences are also non-pushable, so they don't alert. That
answers *"is ISRG at JPM?"* and cannot answer *"what are the key HC conferences this
year?"*.

The `conferences` / `conference_presentations` tables answer the second question. They
existed with the right columns and **zero rows** until 2026-08-05; `src/seed_conferences.py`
now populates them (board #58, JP's ask).

```
python -m src.seed_conferences --dry-run    # print, write nothing
python -m src.seed_conferences              # upsert
```

- **Curated, not scraped, on purpose.** #58 asks for a sell-side-sourced calendar and that
  half is genuinely hard (bank pages inconsistent, several gated). The *medical* circuit is
  stable, published 12–18 months ahead, and is what moves the names — seeding it delivers
  the calendar now and makes the scraper a refinement rather than a prerequisite.
- **The seeder constant is the source of truth, not the table.** DB rows are a pure
  projection of `CONFERENCES` in `src/seed_conferences.py` and the upsert is idempotent, so
  edit there and re-run. This matters more than it looks: `events.db` is gitignored and
  lives between CI runs as an *expiring* GitHub Actions artifact. `events` rebuilds from
  discovery if that artifact is lost — **`conferences` does not, because nothing discovers
  it.** `--conferences-digest` therefore re-seeds on every run, and `--weekly` calls it.
- **The seeder carries NO DDL of its own** (removed 2026-08-12). It routes through
  `init_db`. It used to define the table itself, and that duplicate definition is the
  documented cause of the "17 added / 0 written" incident below.
- **⚠ Dates are evidence, not memory.** Every entry carries `verified`/`unconfirmed` and a
  verification date. **Unconfirmed conferences are NOT written** — a wrong conference date
  silently mis-anchors every catalyst hung off it, which is the one failure a calendar must
  not have. They print each run as work-to-do (currently TCT, SABCS, ACC, ADA, HIMSS,
  AdvaMed, AGBT). 10 seeded, 7 outstanding.
- **The table is STRICTER than it looks** — `name NOT NULL UNIQUE`, `start_date NOT NULL`,
  `end_date NOT NULL`. `CREATE TABLE IF NOT EXISTS` silently no-ops against an existing
  table, so the seeder's first version appeared to define the schema and did not: the dry
  run claimed 17 added and the real run wrote **0**, rolling back on the NOT NULL. Respect
  the constraint; do not relax it.
- **`name UNIQUE` is per INSTANCE, not per series** — "J.P. Morgan 45th Annual Healthcare
  Conference" (`JPM 2027`). Next year's meeting is a *new row* with no intrinsic link to
  this one, which is what `series` (schema v3) exists for: the digest's "Rollover needed"
  section lists any series whose latest instance has gone past-dated, because otherwise the
  calendar just silently empties as the year turns.

### Multi-sector columns (schema v3, 2026-08-12)

Healthcare-only today and it will dominate, but tech and consumer are planned, so
`conferences` gained `sector`, `series`, `host_ticker` while it was still 10 rows.

**`host_ticker` is why company-owned events still live here.** The healthcare circuit is
entirely third-party — ASCO owns ASCO and hundreds of companies present, so `host_ticker`
is NULL on every current row. The tech circuit splits: CES/MWC are neutral, but WWDC is
Apple's and GTC is Nvidia's. Those are *not* routed to `events` even though they have a
ticker, because **`event_type='conference'` is excluded from `PUSHABLE_EVENT_TYPES`** — an
`events` row for a conference fans out nowhere, so routing there buys zero and costs a
calendar split across two tables. One home; fan-out stays a downstream policy call.

**Sector display order** is `Healthcare → Technology → Consumer`, then `Unclassified` for
NULL/unknown (`SECTOR_ORDER` in `src/outputs/slack.py`).

**The dateless backlog grows as sectors expand.** `start_date NOT NULL` is correct and
stays, but it was calibrated to a medical circuit that publishes 12–18 months ahead; tech
and consumer publish later and less regularly. The digest's "Dates needed" section imports
`unconfirmed()` straight from the seeder so that backlog is visible every run instead of
only when someone runs the seeder by hand.

### Output: `#conferences`, not `#analyst-days`

```
python -m src.cli --conferences-digest [--dry-run]     # re-seed + post
```

Separate channel because it answers a different question (which meetings matter, not which
of our companies is doing something). Needs `SLACK_WEBHOOK_CONFERENCES`.

**Unset webhook degrades, it does not fail.** The seed still runs (the half that cannot be
skipped, and the only path that rebuilds this table), the post is skipped, and the reason
is carried into the weekly heartbeat as a warning → `partial`. Deliberately *not* a raise:
one missing secret would otherwise red-line the Monday cron every week and bury real
failures behind a known one. It is still not silent — `partial` plus a named warning lands
in `#status-reports` on every run until the secret exists.

**Until 2026-08-12 this table had no reader at all.** Seeded 2026-08-05, and nothing but
the seeder ever touched it: no CLI mode, no digest, no calendar. Ten verified dates sat in
a gitignored SQLite file that nothing rendered.

### Held instances — the calendar carries history (2026-08-12)

The seeder was **forward-only** until then, so the entire 2025 circuit and all of H1-2026
were missing: meetings that had already happened were recorded nowhere. 15 held instances
were backfilled with web-verified dates (2025 ×10, H1-2026 ×5), taking the table to 25
dated rows across 2025–2027.

That is also what makes `series` load-bearing rather than decorative — `jpm-healthcare`
now has three instances (43rd/44th/45th), `asco` has three, and the digest's rollover
check has real data to work against.

### `kind` — the second axis (schema v4, 2026-08-12)

`sector` says which book a meeting belongs to; `kind` says what actually happens there.
They are orthogonal and the pair is why both exist:

| | Sector | Kind |
|---|---|---|
| ASCO | Healthcare | Scientific meeting (trial data drops) |
| JPM | Healthcare | Investor conference (guidance resets) |
| CAGNY | Consumer | Investor conference |
| CES | Technology | Industry trade show |
| GTC | Technology | Vendor event (one company's launch) |

Collapsed into one field, *"show me every venue where DATA lands"* — which cuts across
sectors — becomes unaskable. `kind` was deliberately deferred while the table had no
reader (designing schema against an imagined query is what left this table empty for a
year); the published page's filters are the concrete consumer that earned it. Current
split: 32 scientific · 5 investor · 2 trade show.

### The dateless backlog is currently EMPTY — the mechanism is not

All seven second-wave series (TCT, SABCS, ACC, ADA, HIMSS, AdvaMed, AGBT) were dated on
2026-08-12 and are now full rows. The `start_date NOT NULL` policy and the digest's
"Dates needed" section both remain live and are pinned by
`test_undated_backlog_renders_when_there_IS_one`, which fakes an undated entry rather
than relying on live data — tech and consumer meetings will land under that policy and it
must not rot while the backlog happens to be empty.

One row shows the policy generalizing past dates: **AGBT 2026 has a NULL location**
because sources disagree (Marco Island vs Orlando) while agreeing on the date. The same
rule that governs dates governs every other field — don't assert a disputed fact.

### The published page

```
python scripts/build_conference_page.py [--years 2025 2026] [--out PATH]
```

Renders `exports/conference_calendar.html` — a self-contained page published as a Claude
Artifact at **https://claude.ai/code/artifact/cc5ff40a-6b3c-49d0-981d-9fdce2c0986e**.
Re-run the script and re-publish **the same file path** to update that URL in place;
publishing a different path creates a second artifact instead.

The page is a *projection* of `data/events.db`, never a hand-maintained copy — that is the
whole reason the generator exists. Artifact CSP blocks every external host, so all CSS and
JS are inline and nothing is fetched.

**Shape:** a quarterly grid — four boxes per year, each listing `Name (dates)` on one line.
Held meetings recede, the current quarter carries an accent rule, and the full name, venue
and rationale ride on each row's `title` so detail is one hover away instead of costing
vertical space. The whole 2025+2026 circuit fits on one screen.

**Filters** across both axes (sector, kind) are plain inline JS. The slug contract between
the chips' `data-value` and the items' `data-sector`/`data-kind` is load-bearing and fails
*silently* if broken — a mismatched chip filters to zero, an unslugged item can never be
shown. `test_published_page_filters_are_wired_to_the_data` pins both directions plus the
element ids the script addresses.

⚠ **Clicks cannot be tested through Claude-in-Chrome.** The artifact renders in an
out-of-process iframe, so synthesized CDP input never reaches it and the a11y tree stops at
the host shell. The interaction was verified by running the page's own script under jsdom
(`npm i --no-save jsdom`, dispatch `click` on the chips, assert visible counts) — worth
repeating that way after changing the script, since neither screenshots nor the Python test
can catch a broken event handler.

**Bookmarked** on `#conferences` as "Conference Calendar". Note the artifact is **private**
by default — the bookmark resolves only for viewers who can open it; share from the page's
share menu to widen that.

## CLI modes

```
python -m src.cli --discover              # Pull EDGAR 8-Ks + Tavily; classify; insert/update events + fan-out
python -m src.cli --remind                # T-30 / T-7 / day-of pings for confirmed events
python -m src.cli --monday-digest         # Monday "forward 30/7" digest → Slack + email
python -m src.cli --friday-digest         # Friday "on the radar" digest → Slack (read-only)
python -m src.cli --conferences-digest    # Re-seed the conference calendar + post it to #conferences
python -m src.cli --weekly                # discover → remind → Monday digest → conference calendar (the Monday cron entry point)
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
- **Confirmation rule**: one authoritative source counts (8-K *or* IR-page press release *or* investor relations site), provided the date is precise AND **grounded in the raw source text** (see Date-grounding gate below). For **pushable (marquee) types**, the source must actually *be* authoritative — a generic `TAVILY_HIT` web hit (not on the company IR domain, not a PR wire) can't single-source-confirm; it stays tentative until an 8-K / IR / press-release source corroborates. Conferences are exempt (tracked-only). See "Source-sensitive bar" below.
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

Discovery does **not** touch the conference calendar. (This line used to claim "conferences
are a parallel iterator over `data/conferences.csv`" — no such file and no such lane has
ever existed in this repo. Corrected 2026-08-12.) Company-anchored conference *events* come
out of the same EDGAR/Tavily pass as everything else; the conference *calendar* is seeded,
not discovered.

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
| `SLACK_WEBHOOK_CONFERENCES` | **New — NOT YET CREATED** | `#conferences` conference calendar. Until it exists the weekly run still seeds the calendar, skips the post, and heartbeats `partial` with a named warning. Create an incoming webhook for `#conferences` on the earnings-agent Slack app, then `gh secret set`. |
| `GOOGLE_CALENDAR_ID` | Dedicated "Other Investing" calendar (floridabusinessman) | Separate from earnings since 2026-05-28 |
| `GOOGLE_CREDENTIALS_JSON` | Reused (earnings_agent) | Service account JSON blob |
| `TICKTICK_ACCESS_TOKEN` | Reused (earnings_agent) | TickTick API |
| `GMAIL_OAUTH_JSON` | Reused (daily-reads) | Full token JSON content; reuses `gmail.send` scope. Locally use `GMAIL_OAUTH_JSON_PATH` instead. |
| `EMAIL_TO` | New | "to" address — `jroypeterson@gmail.com` |
| `SEC_EDGAR_USER_AGENT` or `EDGAR_IDENTITY` | Reused | Required by EDGAR |
| `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` | Reused (earnings_agent / 13F) | Out-of-band failure email backup (inline SMTP in both workflows' `if: failure()` — fires when Slack itself is the failure point). Sends to `jroypeterson+alerts@gmail.com`. Opt-in: unset → no-op. |

CI also sparse-checks out `jroypeterson/Coverage-Manager/exports/` for the watchlist snapshot. The Coverage Manager exports schema gate is `_ACCEPTED_CM_SCHEMA = frozenset({3})` in `src/universe.py` (bumped 2→3 on 2026-06-29 to match CM, mirroring sa-monitor 565af1c). **Adding a CSV column does not bump CM's `EXPORTS_SCHEMA_VERSION`** — the `LEI` and `IPO Date`/`Est Lockup` backfills all propagated into the CSVs and `portfolio.json` entries with no bump; only a change to `universe_metadata.json`'s entry shape bumps it (that is what v3 was). It was briefly widened to `{3, 4}` on 2026-07-28 for an anticipated v4 that was disproven the same night, and narrowed straight back; the frozenset form was kept because the error message names the set. `tests/test_universe_schema.py` pins the gate.

## Local `.env`

Same keys; Google creds via file path (`GOOGLE_CREDENTIALS_PATH=credentials.json`) instead of JSON blob; `COVERAGE_MANAGER_PATH=C:/Users/jroyp/Dropbox/Claude Folder/Coverage Manager`.

## Module map

- `src/cli.py` — CLI entry + top-level flows (`cmd_discover`, `cmd_remind`, `cmd_monday_digest`, `cmd_friday_digest`, `cmd_weekly`, the `*-test` modes).
- `src/universe.py` — Load core watchlist from CM exports; schema version assert.
- `src/discovery/scan_edgar.py` — EDGAR scanner via edgartools. Pulls 8-K (US issuers, Items 7.01/8.01) AND 6-K (foreign private issuers, no item filter) within the lookback window. For each kept filing, walks every HTML attachment (cover doc + Ex-99 exhibits — the press releases where investor-day announcements typically live) and matches against the trigger regex. First hit per filing wins.
- `src/discovery/scan_tavily.py` — Tavily search per ticker.
- `src/discovery/classify.py` — Claude API: extract event_type/dates/multi_day/confidence from raw hits.
- `src/discovery/date_grounding.py` — deterministic date-grounding gate: does the extracted ISO date appear (in any recognizable textual form) in the raw source text? Gates confirmation; see "Date-grounding gate" above.
- `src/seed_conferences.py` — the curated conference calendar (`CONFERENCES` is the source
  of truth) + its idempotent upsert. No DDL of its own; routes through `init_db`.
  _(This slot previously listed `src/discovery/conferences.py`, "Parallel discovery for
  seeded conferences" — a file that has never existed here.)_
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

### Source-sensitive bar (the weak-source guard)

Orthogonal to grounding: a **pushable** event only auto-confirms if it has an **authoritative** source — `8K`, `IR_PAGE`, `PRESS_RELEASE`, or `MANUAL` (`AUTHORITATIVE_SOURCE_TYPES` in `events_repo.py`). A generic `TAVILY_HIT` (web result not on the company's IR domain and not a PR wire) is too weak to single-source-confirm a prep-driving event, so it stays `tentative` until an authoritative source corroborates the same `(ticker, type, date)`. Authoritativeness is accretive on merge (a later 8-K promotes a web-only tentative), and `recompute_statuses` enforces it via `event_has_authoritative_source` (an `EXISTS` over `event_sources`). **Conferences are exempt** — they're tracked-only / never fanned out, and Tavily snippets are their normal signal. `--discover` flags a held event with `! only a generic web source -> held tentative`.

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
  digest failed, Friday DB-not-restored, 0 tickers scanned, or a phase that
  reported failure by return code without raising); `error` = a `--weekly`
  phase raised an exception, or the run aborted before the heartbeat.
- **Phase isolation (`--weekly`).** `_run_phase` wraps each of discover /
  remind / digest, catches `Exception`, records `(phase, traceback)`, and lets
  the next phase run — a bare call handles a phase's *return code*, not an
  *exception*, which is how a `KeyError` in discover took out the entire
  2026-07-27 run **and** its heartbeat. `_post_weekly_health` is called from a
  `finally`, so the heartbeat fires even if the isolation driver itself
  breaks, and it carries `error_text` = the failing phase name + the last
  ~20 traceback lines (`ERROR_TAIL_LINES`), ASCII-sanitized — the BOM that
  caused that abort is *inside* the traceback text. Pinned by
  `tests/test_cli_weekly_isolation.py`.
- **Three distinguishable failure shapes**, not one:
  1. *phase exception* — caught by `_run_phase`; a real `error` heartbeat from
     the CLI naming the phase, with the traceback tail.
  2. *post-heartbeat crash* — `.health/posted` exists, so the workflow
     fallback stands down; the CLI's own heartbeat already told the story.
     ⚠ **One case this oversells** (adversarial review, 2026-07-29): if the
     crash is in a *workflow step after* the phases — the export commit-back or
     the DB save — an `ok` heartbeat has already gone out, so **`#status-reports`
     alone is indistinguishable from a clean run**. The red signal still lands,
     but only in `#analyst-days` and the SMTP backup via `if: failure()`. That
     is within the heartbeat's declared scope (it reports on the weekly phases,
     not on the job's post-processing) and it is still alarmed — but do not read
     a green `#status-reports` as proof the whole job succeeded.
  3. *pre-`main` crash* — no `.health/crash.txt`, because `src/cli.py`'s
     top-level `except BaseException` never got installed. The fallback's
     generic message is now itself the diagnosis (import-time crash, dependency
     install failure, cancelled/timed-out job).
  Anything else that aborts mid-run writes `.health/crash.txt`; both workflows'
  `Health heartbeat fallback` steps `tail -n 20` it into the Slack error block
  instead of posting a constant sentence (HEALTH_REPORTING.md §4.1).
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
