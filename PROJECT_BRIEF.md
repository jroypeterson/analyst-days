# Project Brief — read this first (for reviewers, human or AI)

> 🚧 **Maturity: Work in progress.** This project is partially built / not yet in routine production use. Review it for **direction and approach, not production hardening** — don't over-invest in edge-case, test-coverage, or polish feedback. §2 (status) and §5 (gaps) mark what's intentionally unbuilt.

This file exists so a reviewer can (1) judge how close the project is to its
intended goal and (2) understand the key design decisions **before** giving
feedback. For mechanics — pipeline, schema, CLI modes, secrets, module map — see
`README.md` and `CLAUDE.md`; this brief does not re-describe them.

> When reviewing, weigh findings against the **success criteria** and the
> **non-goals / accepted tradeoffs** below. Several "obvious" extensions (daily
> reminders, conference fan-out, bot-token Slack commands) were deliberately
> deferred or declined — engage the stated rationale before re-proposing them.
> Note also that `README.md`/`CLAUDE.md` describe the *target* design; some of
> it (reminders, Gmail digest, cron) is **not yet built** — see §2 for what is
> actually wired today.

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
| 5 | Dedup + source-provenance so re-runs don't double-post | ✅ Done | `events_repo.py` insert/update/dedupe + provenance merge; one-shot fan-out markers; `tests/test_events_repo.py` (8 tests) |
| 6 | Weekly digests in `#analyst-days` (Monday outlook / Friday radar) | ✅ Done | `--monday-digest` / `--friday-digest` via `slack.py`; Monday digest also emails via `src/outputs/gmail.py` + `src/digest.py` (HTML 30/7-day tables; `--no-email` to skip) |
| 7 | Conferences tracked but kept off prep channels | ✅ Done | `PUSHABLE_EVENT_TYPES` excludes Conference; discovered + stored + visible via `--status`, no Slack/Cal/TickTick |
| 8 | Reminders (T-30 / T-7 / day-of) for confirmed events | ✅ Done | `--remind` drives the state machine in `src/reminders.py`; one-shot transitions stamp `reminded_30_at` / `reminded_7_at` / `day_of_at` |
| 9 | One-shot `--weekly` (discover → remind → digest) cron entry point | ✅ Done | `cmd_weekly` (`src/cli.py`) runs discover → remind → Monday digest in sequence |
| 10 | Runs unattended on a schedule | ✅ Done | `.github/workflows/monday.yml` (`--weekly`) + `friday.yml` (`--friday-digest`); DB persisted as the `analyst-days-db` artifact; `if: failure()` Slack + SMTP backup |

**Overall verdict: the project is built and running unattended (Phase 1, core
watchlist).** The *discovery → classify → fan-out* core, reminders, the email
digest, the `--weekly` orchestrator, and the Monday/Friday GitHub Actions crons
all shipped (2026-06-29). The remaining work is **hardening and reach**, not the
autonomous spine: a `#status-reports` health heartbeat, broader discovery
sourcing, and the Phase 2/3 scale-up — see §5.

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

- **Not full-universe coverage (yet).** Phase 1 is the ~22 `Core=Y` watchlist
  names only. The per-ticker discovery loop is deliberately generalizable so
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
- **Conference list is JPM-seeded only.** Broad conference expansion (ASCO, AACR,
  RSNA, HLTH, …) is backlog.
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
  *Still open follow-on:* a **source-sensitive bar** (let EDGAR/IR single-source
  confirm but require corroboration for generic Tavily hits) — grounding covers
  the worst case; this would tighten the rest.
- **No correction path for a bad confirm.** Dedup is keyed on
  `(ticker, event_type, start_date)`, so a wrong-date confirm followed by the
  correct date becomes a *second* event, not a repair; promotion-only recompute
  never retires the bad one. A `cancelled`/`superseded` status + a retire action
  is the minimal fix. *(Status enum + the export filter are now consistent as of
  2026-06-29; the retire **action** is still TODO.)*
- **Discovery sourcing blind spots.** EDGAR Items 7.01/8.01 + per-ticker Tavily
  systematically miss IR-calendar-only postings (no 8-K) and event names outside
  the trigger regex. *(Regex widened 2026-06-29 to add Innovation/Pipeline/
  Strategy Day + Strategic/Business Update; IR-calendar monitoring still
  unbuilt.)*
- **Scale (Phase 2/3) needs caching + tiered cadence first.** The per-ticker
  Tavily sweep runs unconditionally every week and is the first wall at ~1094
  names (not Anthropic, which only fires on hits). Before scaling: a discovery-
  hit cache (content hash + `last_seen`, classify only new/changed) and tiered
  cadence (core weekly, lower tiers monthly/rotating); a CIK fan-in EDGAR pass
  instead of per-ticker `Company()`. Parallelism is an *after*, not a *before*.
- **No `#status-reports` health heartbeat.** Unlike the rest of the scheduled
  fleet, a failed/partial run is invisible. A `health/v1` Block Kit heartbeat
  should land next.
- **Thin test coverage.** One test file (`tests/test_events_repo.py`, 8 tests)
  covers repo/dedup/promotion. Date-precision parsing, classifier output
  handling, and the reminder transitions are untested.

## 6. How to evaluate

- **Entry points:** `src/cli.py` (modes: `--discover`, `--fanout`,
  `--monday-digest`, `--friday-digest`, `--status`, `--prune-non-pushable`,
  test pings; `--weekly`/`--remind` are stubs). `src/universe.py` loads the
  core watchlist from CM exports.
- **Core logic to scrutinize:** the discovery→classify→confirm path
  (`src/discovery/scan_edgar.py`, `scan_tavily.py`, `classify.py`) and the
  promotion / dedup / fan-out arbitration in `src/state/events_repo.py`. The
  fan-out shaping lives in `src/outputs/{slack,gcal,ticktick}.py`.
- **Tests:** `python -m pytest tests/ -q` (8 tests, `test_events_repo.py`; no
  network needed). Do not run live `--discover` to evaluate — it burns the
  Anthropic + Tavily APIs and hits EDGAR; read the code and use `--dry-run`.
- **Most useful feedback:**
  1. Is single-source confirmation at 0.85 the right risk posture for
     prep-driving events, or should marquee days require corroboration — and is
     a deterministic date-grounding gate (§5) the better lever than the
     confidence bar for the wrong-date failure?
  2. Will the per-ticker discovery primitive hold up at Phase 2/3 scale
     (rate limits, EDGAR/Tavily cost, classifier batching)?
  3. Given limited time, which §5 gap unlocks the most value first — the
     date-grounding/correction posture (protects the core promise) vs. the
     `#status-reports` health heartbeat (makes failures visible) vs. broader
     discovery sourcing (catches more events)?
