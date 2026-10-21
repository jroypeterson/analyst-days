# Project Brief — read this first (for reviewers, human or AI)

> 🚧 **Maturity: Work in progress — but Phase 1 is in routine unattended production** (Monday + Friday GitHub Actions crons since 2026-06-29; every scheduled run 2026-10-09 → 2026-10-19 green). Review the discovery → confirm → fan-out path and the state guard as production code; Phases 2–4 (scale, backfill) are the unbuilt part. §2 (status) and §5 (gaps) mark what's intentionally unbuilt.

> **Staleness -- check before trusting any figure below.** This file is not auto-updated:
> `python ../scripts/audit_project_briefs.py --repo "analyst-days"` counts the behaviour-changing
> commits landed since it was last touched. Section 3 rationale and section 4 non-goals age
> well; section 2 status, and every count, version, path and line number here, is a
> **hypothesis** until checked against the code -- and a disagreement between this file and
> the repo is a defect worth reporting, not a documentation nit.

This file exists so a reviewer can (1) judge how close the project is to its
intended goal and (2) understand the key design decisions **before** giving
feedback. For mechanics — pipeline, schema, CLI modes, secrets, module map — see
`README.md` and `CLAUDE.md`; this brief does not re-describe them.

> When reviewing, weigh findings against the **success criteria** and the
> **non-goals / accepted tradeoffs** below. Several "obvious" extensions (daily
> reminders, conference fan-out, bot-token Slack commands) were deliberately
> deferred or declined — engage the stated rationale before re-proposing them.
> `README.md`/`CLAUDE.md` once described a *target* design ahead of the code;
> the pieces this note used to list as unbuilt (reminders, Gmail digest, cron)
> all shipped 2026-06-29 — see §2 for what is actually wired today.

---

## 1. Intended goal (the "why")

Give the user a **standing radar for the prep-worthy investor-facing events**
across their coverage universe — Investor Days, Analyst Days, R&D Days, Capital
Markets Days — so a single solo, part-time investor never gets surprised by one
and can plan reading/work around it. The user can't manually watch ~22 core
names (let alone the full universe) for these announcements, which surface
unevenly across 8-Ks, IR pages, and press chatter. The tool's job is to **turn
that noise into a small, confirmed, calendar-ready signal** and push it to where
the user already lives: Slack, Google Calendar, and TickTick.

Context: this is one node in the user's "signal from noise" workspace.
Coverage Manager owns *which* tickers matter; this tool owns *when* their
marquee events happen. Success is not "find every event" — it's "surface the
few that warrant prep, with the right date, without false-alarming the
calendar."

## 2. Success criteria — and current status

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Discover marquee events from authoritative + web sources | ✅ Done | `--discover`: EDGAR 8-K (Items 7.01/8.01) + 6-K incl. Ex-99 attachments via edgartools, plus per-ticker Tavily. `src/discovery/scan_edgar.py`, `scan_tavily.py` |
| 2 | Classify type/date/precision/confidence with an LLM | ✅ Done | Sonnet 4.6 extractor in `src/discovery/classify.py`; per-type confidence bars (Investor/Analyst/R&D/CMD = 0.85, Conference 0.70) in `events_repo.py` |
| 3 | Only confirmed, precise-date events fan out; imprecise stay tentative | ✅ Done | `recompute_statuses` promotion-only; imprecise dates ("Q3 2026") never auto-confirm; surfaced in digest only |
| 4 | Fan confirmed events out to Slack + Calendar + TickTick | ✅ Done | `--fanout` + `_fan_out_confirmed`; `src/outputs/{slack,gcal,ticktick}.py`; type-prefixed titles; multi-day all-day blocks |
| 5 | Dedup + source-provenance so re-runs don't double-post | ✅ Done | `events_repo.py` insert/update/dedupe + provenance merge; one-shot fan-out markers. **Idempotent across a lost DB since `b305080` (2026-10-05, #456):** Calendar adopted by a natural-key extendedProperty, TickTick by a key content line, Slack suppressed on adoption (`tests/test_fanout_idempotent.py`). Before that, `gcal.find_existing_by_event_key` had zero callers and a rebuilt DB re-posted everything |
| 6 | Weekly digests in `#analyst-days` (Monday outlook / Friday radar) | ✅ Done | `--monday-digest` / `--friday-digest` via `slack.py`; Monday digest also emails via `src/outputs/gmail.py` + `src/digest.py` (HTML 30/7-day tables; `--no-email` to skip) |
| 7 | Conferences tracked but kept off prep channels | ✅ Done | `PUSHABLE_EVENT_TYPES` excludes Conference; discovered + stored + visible via `--status`, no Slack/Cal/TickTick |
| 8 | Reminders (T-30 / T-7 / day-of) for confirmed events | ✅ Done | `--remind` drives the state machine in `src/reminders.py`; one-shot transitions stamp `reminded_30_at` / `reminded_7_at` / `day_of_at` |
| 9 | One-shot `--weekly` cron entry point | ✅ Done | `cmd_weekly` (`src/cli.py`) runs discover → remind → Monday digest → conference calendar, each phase isolated by `_run_phase` so one exception cannot skip the rest or the heartbeat (`b9520b6`, 2026-07-29) |
| 10 | Runs unattended on a schedule | ✅ Done | `.github/workflows/monday.yml` (`--weekly`) + `friday.yml` (`--friday-digest`); DB persisted as the `analyst-days-db` artifact; `if: failure()` Slack ping (the SMTP backup runs only if `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` are set — neither is a repo secret on 2026-10-20, so that backup is currently a no-op). CI reads CM exports from the **private** `jroypeterson/coverage-manager-private` repo via the `CM_DEPLOY_KEY` deploy key (`d68a47a`, 2026-10-05, #517) |
| 11 | A lost events DB fails loudly instead of silently restarting | ✅ Done | `src/state_guard.py` (`b305080`, #456): the Monday run exits 1 unless `events` has ≥ 1 row; the DB save is gated on it; a fresh start needs the one-run `bootstrap_db=true` dispatch input |
| 12 | Conference calendar ("what are the key HC meetings"), conference-anchored | ✅ Done | `src/seed_conferences.py` curated, web-verified seed (board #58, `1ba96d3` 2026-08-05; held 2025/H1-2026 instances `03487e6`); `--conferences-digest` → `#conferences`, re-seeded every Monday; published page `scripts/build_conference_page.py` (`6662f36`, `0523488`) |
| 13 | Analyst-day listing page (last year + this year) that states its own gaps | ✅ Done | `scripts/build_analyst_day_page.py --fetch` (`7d97d52`, 2026-10-04, board #439) renders from the fetched CI artifact, never the local sandbox DB; published as a private Claude Artifact |
| 14 | Published upcoming-events feed for sibling lanes | ✅ Done | Monday commits back `exports/upcoming_events.json` (sa-monitor's halt-alert `Note:` enrichment fetches it from raw.githubusercontent) — an interface, not a scratch file |

**Overall verdict: the project is built and running unattended (Phase 1).** The
*discovery → classify → fan-out* core, reminders, the email digest, the `--weekly`
orchestrator, and the Monday/Friday GitHub Actions crons all shipped (2026-06-29);
the health heartbeat, date-grounding gate, source-sensitive bar and `--retire`
followed 2026-06-30, and the state guard + idempotent fan-out 2026-10-05. Phase 1's
scan set is now **watchlist `Core=Y` ∪ Following for Interest** (`b29caec`,
2026-08-23 — FFI deliberately *not* gated on Core; 75 names measured 2026-10-04),
not the ~22 core names this brief originally described. The remaining work is
**reach** — broader discovery sourcing, Phase 2/3 scale-up, the Phase 4 historical
backfill — see §5.

## 3. Key design decisions (and why)

1. **Confirmation is single-source, not consensus.** One authoritative source
   (8-K *or* IR press release *or* IR site) is enough to confirm. For a
   prep-planning tool, waiting for corroboration would mean missing the prep
   window; the failure mode (an occasional spurious confirm) is cheaper than a
   late one.
2. **Per-type confidence bars, not one global threshold.** Marquee days sit at
   0.85 because a *wrong-date* confirm makes the user prep on the wrong day —
   the expensive error. Conferences sit at 0.70 because their worst case is a
   spare calendar entry, not misdirected prep. Imprecise dates never auto-confirm
   at any threshold.
3. **Conferences are tracked-only, not fanned out.** They're discovered,
   classified, and stored (visible via `--status`) but excluded from Slack /
   Calendar / TickTick via `PUSHABLE_EVENT_TYPES`, because they don't drive
   single-company prep the way a named Analyst Day does. Policy is one constant +
   a `--prune-non-pushable` cleanup, so it's trivially reversible.
4. **Multi-destination fan-out, each with a job.** Slack = the per-confirm ping +
   digest feed; Calendar = the durable blocked time (multi-day → all-day block);
   TickTick = the actionable to-do. The same confirmed event is shaped per
   channel rather than dumped identically everywhere.
5. **Promotion-only status recompute.** Tightening a threshold later never
   demotes an already-fanned-out event (that would orphan calendar/TickTick
   entries). To remove an event you delete the row, not lower its status.
6. **SQLite as workflow memory + provenance.** `data/events.db` is the single
   referee for dedup, one-shot fan-out markers, and source history — so re-runs
   are idempotent and the same event accreting sources over time stays one row.
7. **Reuse the fleet's existing auth.** Calendar via the shared earnings-agent
   service account on the dedicated "Other Investing" calendar; TickTick + (planned)
   Gmail tokens reused from earnings_agent / daily-reads. New surface added only
   where unavoidable (Anthropic key, the `#analyst-days` webhook).

## 4. Non-goals / accepted tradeoffs

- **Not full-universe coverage (yet).** Phase 1 is watchlist `Core=Y` ∪
  Following for Interest (75 names on 2026-10-04); the #439 page counts the 277
  Core-coverage tickers that are *not* scanned rather than hiding them. The per-ticker discovery loop is deliberately generalizable so
  Phases 2/3 swap the iterator (HC Services + MedTech, then ~1094 full universe)
  without re-architecting. Don't critique the *scale* — critique whether the
  per-ticker primitive scales.
- **Not real-time.** Discovery is a once-a-week batch by design; an event
  announced Tuesday surfaces on the following Monday fire. Acceptable for events
  that are weeks/months out.
- **Not a webcast/replay-link or per-slot ("Day 2, 14:30") tracker.** Explicitly
  backlog, not v1.
- **No Slack-reply commands** (`lock`/`snooze`/`ignore`). That requires migrating
  from an incoming webhook to a bot token + `conversations.history`; deferred as
  not worth the auth surface yet.
- **The conference calendar is curated, not scraped.** `CONFERENCES` in
  `src/seed_conferences.py` is the source of truth (medical circuit incl. ASCO,
  JPM, ACC, ADA, HIMSS…; an undated entry is refused, never guessed). A sell-side
  conference scraper (board #58's other half) is deliberately deferred.
  *(This line said "JPM-seeded only" until 2026-10-20 — false since 2026-08-05.)*
- Owns *event timing*, not the universe — it reads CM's exports and does not
  invent membership.

## 5. Known gaps / candidate next steps (feedback welcome here)

> The autonomous spine (scheduler, reminders, email digest, `--weekly`) shipped
> 2026-06-29 and is reflected in §2. The remaining gaps below are about
> **correctness posture and reach**, not "is it running."

- **Wrong-date confirms — date-grounding gate SHIPPED 2026-06-30.** Promotion
  used to check only date-presence + LLM self-confidence, so a classifier that
  extracted the *wrong* date from a *real* announcement still auto-confirmed (the
  "prep on the wrong day" failure §3.2 calls the expensive error). Now a precise
  date only confirms if it's **grounded in the raw source text** —
  `src/discovery/date_grounding.py` renders the ISO date into the forms a filing
  actually uses (long/abbrev month, day-first, M/D/Y, ISO) and word-boundary-
  matches them against the EDGAR excerpt / Tavily snippet (year required for
  month-day-only mentions). Ungrounded precise dates stay `tentative` (radar-only)
  regardless of confidence; a later grounded source promotes them. Persisted as
  `events.date_grounded` (schema v2) so `recompute_statuses` honors it too.
  The companion **source-sensitive bar** also shipped (2026-06-30): a pushable
  event needs an authoritative source (8-K / IR / press release) to confirm; a
  generic `TAVILY_HIT`-only event stays tentative until corroborated.
  Conferences are exempt.
- **Correction path for a bad confirm — manual retire SHIPPED.** Dedup is still
  keyed on `(ticker, event_type, start_date)` (`UNIQUE` in `src/state/schema.py`),
  so a wrong-date confirm followed by the correct date is a *second* row. The fix
  is `--retire TICKER TYPE DATE [--retire-as cancelled|superseded]`
  (`cmd_retire`), which tears down the Calendar + TickTick entries and sets a
  terminal status that recompute, digests and the export skip. **Still manual:**
  nothing detects a superseded pair automatically, and a Slack ping already sent
  cannot be unsent (webhook).
- **Discovery sourcing blind spots.** EDGAR Items 7.01/8.01 + per-ticker Tavily
  systematically miss IR-calendar-only postings (no 8-K) and event names outside
  the trigger regex. *(Regex widened 2026-06-29 to add Innovation/Pipeline/
  Strategy Day + Strategic/Business Update; IR-calendar monitoring still
  unbuilt — no IR-page scanner exists in `src/discovery/` on 2026-10-20.)*
- **No history before 2026-07-06.** `classify.py` drops past events and
  `status='historical'` is defined but never set, so the #439 page shows 2025 as
  "not searched". Closing it is Phase 4 and needs a past-events classifier mode.
- **`#conferences` post is skipped every Monday.** `SLACK_WEBHOOK_CONFERENCES` is
  not a repo secret (checked 2026-10-20), so the weekly run seeds the calendar,
  skips the post and heartbeats `partial` with a named warning — by design, but
  the Monday heartbeat cannot read `ok` until that webhook exists.
- **Restore takes only successful runs.** A red Monday run's DB is discarded, so a
  failing job rolls state back a week, and 13 straight failed Mondays expire every
  restorable artifact. Taking the newest artifact regardless of conclusion (as
  earnings_agent does) is the follow-up noted in `CLAUDE.md`, not done.
- **Scale (Phase 2/3) needs caching + tiered cadence first.** The per-ticker
  Tavily sweep runs unconditionally every week and is the first wall at ~1094
  names (not Anthropic, which only fires on hits). Before scaling: a discovery-
  hit cache (content hash + `last_seen`, classify only new/changed) and tiered
  cadence (core weekly, lower tiers monthly/rotating); a CIK fan-in EDGAR pass
  instead of per-ticker `Company()`. Parallelism is an *after*, not a *before*.
- **`#status-reports` health heartbeat — SHIPPED 2026-06-30.** Both scheduled
  runs now post a `health/v1` Block Kit heartbeat (`src/health.py`), with an
  `if: always()` workflow fallback for the run-aborted case. ok/partial/error
  semantics; `--health-test` verifies the webhook. A failed/partial run is no
  longer invisible.
- **Test coverage is no longer thin** (one file / 8 tests at 2026-06-30): 17
  files / 179 test functions on 2026-10-20, covering date grounding, reminders,
  scanners, the state guard, fan-out idempotency and the listing page. Not
  covered: classifier output handling against a real model response.

## 6. How to evaluate

- **Entry points:** `src/cli.py` (modes: `--discover`, `--fanout`,
  `--monday-digest`, `--friday-digest`, `--conferences-digest`, `--weekly`,
  `--remind`, `--retire`, `--status`, `--prune-non-pushable`, test pings — none
  is a stub, though this line called `--weekly`/`--remind` stubs until
  2026-10-20). `src/universe.py` loads Core=Y ∪ FFI from CM exports.
- **Core logic to scrutinize:** the discovery→classify→confirm path
  (`src/discovery/scan_edgar.py`, `scan_tavily.py`, `classify.py`) and the
  promotion / dedup / fan-out arbitration in `src/state/events_repo.py`. The
  fan-out shaping lives in `src/outputs/{slack,gcal,ticktick}.py`.
- **Tests:** `python -m pytest tests/ -q` (17 files; no network needed). Do not run live `--discover` to evaluate — it burns the
  Anthropic + Tavily APIs and hits EDGAR; read the code and use `--dry-run`.
- **Most useful feedback:**
  1. Is single-source confirmation at 0.85 the right risk posture for
     prep-driving events, or should marquee days require corroboration — and is
     a deterministic date-grounding gate (§5) the better lever than the
     confidence bar for the wrong-date failure?
  2. Will the per-ticker discovery primitive hold up at Phase 2/3 scale
     (rate limits, EDGAR/Tavily cost, classifier batching)?
  3. Given limited time, which §5 gap unlocks the most value first — automatic
     superseded-pair detection (protects the core promise), broader discovery
     sourcing (IR calendars; catches more events), or the Phase 4 historical
     backfill (gives the listing page a 2025)? *(Date grounding and the health
     heartbeat, the earlier candidates here, both shipped 2026-06-30.)*
