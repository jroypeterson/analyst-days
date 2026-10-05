# #456 — a lost DB artifact must not silently restart state; fan-out must be idempotent

Board row #456 (quarterly fleet review, Codex 2026-09-23, `codex_feedback/codex_feedback_2026-09-23_132228.md`
findings 2, 3, 7). Scope here is those three. Findings 1, 4, 5, 6, 8 (grounding fallback,
multi-day convergence, undated-sibling merge, `--retire` teardown, EDGAR exhibit read errors)
are NOT in this change; the ship-log entry is `--partial`.

## Reproduced against HEAD 84c39b6 (fakes only, no network)

| # | Defect | Reproduction |
|---|---|---|
| 1 | Missing artifact → silent fresh state | `monday.yml` restore uses `if_no_artifact_found: warn`; `init_db()` on a nonexistent path creates an empty DB with no error; `Save events database` (`if: always()`) uploads it as the newest `analyst-days-db`. Worse than "discover rebuilds": if discover raises, the `conferences` phase still calls `init_db`, so the uploaded DB can be conferences-only. |
| 2 | Rebuild → duplicates | Two DBs built from the same event, fanned out against one fake Calendar + fake TickTick: **2 calendar events, 2 TickTick tasks, 2 Slack pings**. `find_existing_by_event_key` has 0 callers in `src/`. |
| 3 | Fan-out failures invisible | Slack/Calendar/TickTick all raising → `_fan_out_confirmed` returns `{fanout_slack:0, fanout_gcal:0, fanout_ticktick:0}`, no error field; `cmd_discover` returns 0; heartbeat `ok`. |

## Facts measured 2026-10-04 (read-only `gh api`)

- 12 live `analyst-days-db` artifacts, newest 2026-09-28, all master, `retention-days: 90`; the
  repo's retention setting is 90 days and its **maximum allowed is 90** — cannot be raised.
- Last 8 scheduled Monday runs: all `success`.
- Restore selects `workflow_conclusion: success`. So the realistic loss path is **13 consecutive
  weeks with no successful master Monday run** (every artifact a success run uploaded has
  expired), or a manual deletion. Rare, but when it happens it is silent today.

## Design

### A. State guard — fail loudly unless a fresh start is explicitly requested

**Polarity deliberately inverted from the precedent.** earnings_agent's
`EA_CONSENSUS_BOOTSTRAPPED` is a repo variable whose *absence* leaves the guard inert, and JP
arms it after the first upload. That fit a lane with no artifact yet. Here the artifact has
existed since 2026-07-06 and production must pass without JP doing anything, so the guard is
**armed by default** and the escape hatch is the explicit, rare action.

The escape hatch is a **`workflow_dispatch` input `bootstrap_db`** (default `"false"`), not a
repo variable. Reason: a variable left set after a bootstrap silently re-opens the hole on
every subsequent scheduled run — exactly the "inert window forgotten for ~85 days"
(`EA_DB_BOOTSTRAPPED`) the precedent documents. A dispatch input cannot outlive its run, and
scheduled runs cannot set it. Mirrored from the precedent: value normalised (trim, lowercase)
and compared to exactly `true`; the alarm lands in `#status-reports` + the failure paths.

Two layers, because each covers a hole the other cannot:

1. **Workflow step `Require events database`** (id `require_db`), right after restore. If
   `data/events.db` is missing or empty and `bootstrap_db != true`: write a specific message to
   `.health/crash.txt` (so the existing fallback heartbeat quotes it instead of the generic
   "died before the handler" text), `::error::`, `exit 1`. That fails the job, which fires the
   existing `Notify Slack on failure` (#analyst-days) + `Email backup on failure`, and the
   fallback heartbeat posts `error` to `#status-reports`. `Run weekly` is skipped.
2. **Python guard in `cmd_weekly` and `cmd_discover`** (`_require_existing_db`): under CI
   (`GITHUB_ACTIONS=true`), if `args.db` does not exist and `ANALYST_DAYS_BOOTSTRAP_DB` is not
   `true`, raise before any phase opens the DB. This is the runtime, unit-testable layer and
   survives someone deleting the workflow step. Locally (no `GITHUB_ACTIONS`) a fresh dev DB
   stays allowed. The workflow maps `ANALYST_DAYS_BOOTSTRAP_DB` from the dispatch input only.
3. **`Save events database`** gains `steps.require_db.outcome == 'success'` so a run that
   failed the guard can never upload anything as state (today the `always()` upload would
   publish whatever exists).

Bootstrap semantics: `bootstrap_db=true` *permits* a fresh DB; it does not force one. If an
artifact was restored anyway, the run uses it and prints a notice. When a fresh DB *is* created
under bootstrap, the weekly heartbeat carries a warning ("events DB bootstrapped fresh — history,
reminder stamps and output ids restart today") → `partial`, so it is never mistaken for a
normal week.

### B. Idempotent fan-out — re-attach, never blind-insert

- **Calendar:** in `upsert_calendar_event`, when the row has no `calendar_event_id`, call
  `find_existing_by_event_key` first. Found → `update` that event in place and persist its id
  (adopt). Not found → insert as today. **A lookup error propagates** (counted as a Calendar
  failure, retried next run) — never fall through to an insert we cannot prove is unique.
  The key is `ticker|type|start|end`; a rebuilt row whose end date differs from the original's
  will not match (known limit, same as the key's existing design).
- **TickTick** has no extended properties. New tasks get a trailing marker line in `content`:
  `analyst-days key: <same natural key>`. Before creating, `GET /project/{list}/data` (undone
  tasks) and adopt a task whose content carries the marker; legacy fallback for tasks created
  before this change: exact title (`[Investor Day] CI`) AND due-date day == `start_date`.
  Adopt → update in place (which also writes the marker) + persist id. Lookup error propagates.
  Known limit: the endpoint returns only *undone* tasks, so a task JP already completed is not
  found and would be re-created after a rebuild.
- **Slack has no lookup** (webhook). Rule: if, for a row with `slack_posted_at` NULL, Calendar
  or TickTick *adopted* an existing artifact, that is proof the event was fanned out before →
  set `slack_posted_at`, do not re-ping. To have that evidence the per-row order becomes
  Calendar → TickTick → Slack. Edge accepted: a prior run whose Calendar succeeded but Slack
  failed would have its ping suppressed after a rebuild (the event is on the calendar either way).
  When neither Calendar nor TickTick ran (`--no-gcal --no-ticktick`, or both down) there is no
  evidence and the ping goes out as today.

### C. Fan-out failures reach the exit code and the heartbeat

- `_fan_out_confirmed` additionally returns `fanout_errors` (int) and `fanout_failures` (list of
  short ASCII strings, e.g. `"Calendar auth: RuntimeError"`, `"TickTick CI: TickTickError"`).
  Auth/list failures count once per channel plus are named; rows that were never attempted
  because the channel was disabled are counted so the number means "rows that did not reach a
  surface they should have".
- `cmd_discover` and `cmd_fanout` return 1 when `fanout_errors > 0`.
- `_post_weekly_health` adds `partial` + a named warning (`fan-out failed: …`, capped list) and
  the errors counter includes them. The discover rc=1 also makes `cmd_weekly` return 1 → the
  workflow goes red and the failure Slack/email fire. Deliberate: unlike the missing
  `#conferences` webhook (a known, static gap), a Calendar/TickTick failure means a prep surface
  silently lacks a confirmed event.

## What JP must do

Nothing for the current state (artifact present → guard passes). Only if the artifact is ever
genuinely lost: run `monday.yml` via the Actions button with `bootstrap_db=true`, once.

## Tests (fakes only; each mutation-checked)

1. Python guard: CI + missing DB + no bootstrap → raises before `init_db` creates the file
   (assert file still absent); with bootstrap → proceeds + heartbeat warning; local → allowed;
   existing DB → passes.
2. Workflow (yaml parse): `require_db` step exists after restore and before `run_weekly`;
   `Save events database` gated on `steps.require_db.outcome == 'success'`; bootstrap env comes
   only from `github.event.inputs.bootstrap_db`; scheduled path cannot set it. Also execute the
   `require_db` shell under bash against a temp dir (missing → rc 1 + crash.txt; present → 0;
   bootstrap → 0).
3. Rebuild idempotency: the reproduction above as a test — two DBs, one fake Calendar/TickTick
   → exactly 1 event, 1 task, 1 Slack ping; second DB's row ends with the adopted ids.
   Legacy TickTick task (no marker) adopted by title+due date. Lookup raising → no insert, error
   counted.
4. Failure accounting: all channels raising → `fanout_errors` > 0, named failures, `cmd_fanout`
   rc 1, weekly heartbeat `partial` with the warning.

---

## Round 1 (Fable 5.1, `claude-fable-5-1`) — dispositions

Review: `plans/456_fable_review_r1_2026-10-04.md`. Two High, four Medium, four Low.

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | High | §C makes fan-out errors fail the job; restore reads only `success` runs → a fan-out error rolls state back a week (dup reminders, lost rows); a 180-day TickTick expiry then becomes 13 red weeks → total loss | **§C revised: fan-out failures do NOT change the weekly exit code.** They reach the heartbeat as `partial` + a named warning on every run until fixed (the `#conferences`-webhook precedent: a known channel outage must not red-line the job). `--fanout` (manual) still returns 1. The underlying rollback — *any* failed Monday (e.g. a reminder post error, rc 1 today) discards that week's DB because restore filters on `success` — is **pre-existing and out of scope**; switching restore to newest-by-created_at also changes `build_analyst_day_page.py` (#439, shipped today with tests built on the success filter). Logged as a follow-up, not silently widened. |
| 2 | High | Existing update path blind-inserts on any update error (gcal any exception; TickTick any ≥400) | **Fixed in scope.** Re-create only when the stored id is provably gone (gcal HTTP 404/410; TickTick 404), and even then via the key lookup first. Every other error propagates and is counted. |
| 3 | Med | `-s` accepts a conferences-only DB | Guard predicate = **`events` table has ≥ 1 row** (stdlib `sqlite3`, read-only `mode=ro` URI). Not `min(first_seen_at) <= HISTORY_FLOOR`: after any legitimate bootstrap that floor is permanently violated and the guard would block every run forever. Cost: a bootstrap that finds 0 events fails the next Monday loudly; re-dispatch with `bootstrap_db=true`. |
| 4 | Med | guard placement | Primary site `cmd_weekly`, inside the `try` before phase 1 (heartbeat still fires via `finally`); `cmd_discover` keeps it for direct `--discover`. Same predicate as the workflow (missing OR zero event rows). "DB absent at start" captured before any phase for the bootstrap warning. |
| 5 | Med | deleted/retired calendar entries invisible to lookup | `showDeleted=true`. A `cancelled` match = fanned out before, then removed → no insert, `slack_posted_at` set, cancelled id NOT stored, a counted warning (not an error). Live match preferred over cancelled. |
| 6 | Med | accounting ambiguity | `--no-*` flags are never errors. A disabled channel (auth/list/expired token) counts once, named, plus the number of rows it left unposted in the message text. Moot dedupe (no rc change). |
| 7 | Low | fallback header says `Run weekly (outcome: skipped)` | Pass `REQUIRE_DB_OUTCOME`; header names the guard step when it failed. Dry-run dispatch skipping the fallback is accepted (stated). |
| 8 | Low | env mapping / YAML `on` → `True` | Input mapped via `env:` in both steps; tests index `data[True]`. |
| 9 | Low | TickTick fetch once; legacy due-date tz | Fetch undone tasks once per run (cached on first need). Legacy fallback accepts due-date within ±1 day of `start_date` (same title = same ticker+type, so the window cannot adopt a different event type). |
| 10 | Low | friday.yml wording | Reworded: missing DB on Friday is "artifact not restored — expired or lost?", still `partial`. |

## Round 2 (Fable 5.1) — no Critical/High; dispositions applied

Review: `plans/456_fable_review_r2_2026-10-04.md`. Applied: `schema_meta.bootstrapped_at` stamp +
amber "restarted on <date>" banner on the #439 page; a cancelled Calendar match gates TickTick and
Slack too; `Save events database` also skipped on `dry_run`; TickTick task ids already held by
another row are never adopted; `cmd_fanout` rc-1 vs `cmd_discover` rc-0 asymmetry commented.

## Codex code review (gpt-5.6-sol; transcripts are gitignored in this public repo, kept locally)

| Round | Lens | Finding | Outcome |
|---|---|---|---|
| 1 (2026-10-05 00:05) | correctness | **P1** TickTick adoption called `update_task`, whose single-task GET `/task/{p}/{id}` 404s for valid ids in production (earnings_agent verified 8/8) — adoption would fail every Monday; the test replaced `update_task` wholesale, so it could not see it | Fixed: adopt writes the LISTED object (`write_task`); `update_task` reads via `/project/{id}/data`; a stored id no longer open is left alone (completed vs deleted indistinguishable). Fake rewritten at the `requests` level with the 404 quirk encoded. |
| 2 (00:23) | resilience | **P1** a cancelled match only skipped fan-out; the row stayed `confirmed`, so the remind phase and digests/export would still ping/publish a retired event | Fixed: row auto-retired as `cancelled` (terminal) with an undo SQL line in `events.notes`, named in the heartbeat. Reverses the round-1 "name, don't auto-retire" choice. |
| 3 (00:31) | new angle | "No actionable Critical or High correctness/safety defects" | clean |

Tests: 197 passed. Mutations: 28 applied (guard predicate, CLI guard sites, workflow wiring, Calendar
lookup / showDeleted / 404-only, TickTick lookup / claimed ids / single-GET / no-recreate, Slack
suppression, error counting, heartbeat, page banner, quarantine) — all killed.
