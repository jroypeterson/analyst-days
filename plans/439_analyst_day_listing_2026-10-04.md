# #439 — Analyst-day listing page (2025 + 2026) — plan

Board row #439 (JP, 2026-09-17, `#project-ideas`): *"For the analyst-days channel and
project, add an artifact listing every analyst day from this year and last year for core
coverage, portfolio and researching companies."*

Status: PLAN r3 — revised after Fable rounds 1 and 2 (`439_fable_review_r1_2026-10-04.md`, `439_fable_review_r2_2026-10-04.md`); round 2 had no Critical/High.

## 1. Recon — what the data can and cannot answer (measured 2026-10-04)

| Fact | Evidence |
|---|---|
| The real events DB is the CI artifact `analyst-days-db`, not `data/events.db` | Local `data/events.db` is an April-2026 dev sandbox: 3 rows, all `first_seen_at` 2026-04-29. Latest artifact (run 36473652121, 2026-09-28): 55 rows. |
| Recorded history starts **2026-07-06** | `min(first_seen_at)` = 2026-07-06T15:54Z in every live artifact (11 weekly snapshots 2026-07-13 → 2026-09-28, counts monotonic 9 → 55). No reset since then. The 2026-07-06 artifact itself has expired (90-day retention). `run_log` table exists but has **zero rows** — no run history to lean on. |
| Analyst-day-type rows (investor / analyst / R&D / capital markets day): **17 rows, 15 tickers** | 38 of the 55 rows are `conference` (a company presenting at a conference — not an analyst day). |
| **2025: zero analyst-day rows.** 2026: 10 dated. 2027: 2. Undated (imprecise): 5 | **By classifier design:** `src/discovery/classify.py` rule 1 — *"Future events only… Exclude past events entirely"* — so any 2025 hit that surfaced would be dropped. `status='historical'` exists in the enum and nothing sets it. Also, discovery is forward-looking: 8-K/6-K 14-day lookback + Tavily `"<company>" "investor day" … <current year>`. Past events appear only when a forward search incidentally surfaced them (FDX 2026-04-08, TSM 2026-04-25, SPGI 2026-05-12, AMZN 2026-06-14). Phase 4 historical backfill was never built. |
| Several stored rows are low-confidence leads, some visibly wrong-issuer | ADI 2026-07-14 is *Resideo's* "ADI Global Distribution" investor day (conf 0.62); CP 2026-11-12 sourced from a Boerse Stuttgart page for Brenntag (0.50); VEEV 2027-05-20 is the customer R&D Summit (0.72); AMZN 2026-06-14 from a MarketScreener calendar (0.45). All are `discovered`/`tentative`, never confirmed. |
| What the lane scans | `src.universe.load_core_watchlist()` = watchlist `Core=Y` (53) ∪ Following-for-Interest (22) = 75 tickers today. |
| JP's three lists vs what is scanned (measured 2026-10-04; the page recomputes at build) | Portfolio 37 + Researching 35 = 72 (= `watchlist.csv`). **19 of those 72 are `Core≠Y` and are not scanned.** "Core coverage" = `universe.csv Core=Y` = 345 names; **277** are not scanned. 7 scanned names are FFI-only and not Core (CENTA, CHWY, FRPT, LULU, PETS, TSM, WOOF). |
| Repo visibility | `analyst-days` is PUBLIC, and so is Coverage-Manager, which already tracks `exports/portfolio.json` / `researching.json` / `following_for_interest.json`; `upcoming_events.json` here is committed too. **The page adds no exposure.** It is still kept private + gitignored as a conservative default (and so a generated view is not a second committed copy that drifts). Whether CM's exports should be public at all is a separate question for JP, out of scope here. |

**Consequence:** the DB cannot answer "every analyst day from 2025 and 2026" today. A page
that renders it plainly would show ~10 rows titled as two years and imply completeness —
exactly the failure to avoid. The deliverable is therefore *the listing plus an explicit
coverage ledger*, and the history gap is a stated follow-up, not something papered over.

## 2. What I'll build

`scripts/build_analyst_day_page.py` — a projection of the CI events DB + current CM lists
into one self-contained HTML page, published as a private claude.ai Artifact (the
`conference_calendar.html` precedent: script → gitignored HTML → re-publish the same path
to keep the URL).

### 2.1 Inputs
- `--fetch`: list artifacts with `gh api repos/jroypeterson/analyst-days/actions/artifacts?name=analyst-days-db`,
  keep `expired=false` + `workflow_run.head_branch=master`, and for each newest-first ask
  `gh run view <run> --json conclusion`; take the first with `conclusion=success`
  (the same selection Monday's restore uses — `monday.yml` uploads `if: always()`, so the
  newest artifact can be from a failed run that production will discard). Download with
  `gh run download <run> -n analyst-days-db` into `data/ci_snapshot/` (gitignored) and
  write `provenance.json` {artifact_id, artifact_created_at, run_id, run_url,
  run_conclusion, fetched_at, skipped_newer_failed: [{artifact_created_at, run_id,
  run_url}]}. Every `gh` call passes `-R jroypeterson/analyst-days` (one constant) so the
  script works from any cwd. The script calls `load_dotenv()` itself (scripts/ do not
  inherit `src/cli.py`'s). `gh` is free (no metered API).
- `--db PATH` (default `data/ci_snapshot/events.db`). If the default is missing the script
  exits non-zero with "run --fetch" — it **never falls back to `data/events.db`** (the
  stale dev sandbox would render 3 plausible rows). If an explicit `--db` has no
  `provenance.json` beside it, the page carries a red "unverified source DB" banner.
- CM lists via `COVERAGE_MANAGER_PATH` (env, `.env` fallback): `portfolio.json`,
  `researching.json`, `following_for_interest.json`, `universe.csv` (`Core=Y`). Scanned
  set = `src.universe.load_core_watchlist()` **called directly** (no reimplementation, so
  it cannot drift from what discovery actually iterates).

### 2.2 Row selection — every stored row is shown or counted, never silently dropped
Over **all** `events` rows, each lands in exactly one bucket, decided by this
**precedence** (first match wins):

| # | Bucket | Rule |
|---|---|---|
| 1 | Counted — retired | status ∈ {cancelled, superseded} (count + tickers) |
| 2 | Counted — conference appearance | `event_type = conference` (not an analyst day; count only) |
| 3 | Counted — unknown type | `event_type` not in the four analyst-day types (defensive; 0 today) |
| 4 | **Shown — date not yet known** | `start_date IS NULL OR date_imprecise = 1` (renders `imprecise_hint`, never a false-precise date) |
| 5 | **Shown — listing** | `start_date` year ∈ {last year, this year} |
| 6 | Counted — outside the two years | anything else dated (count + "TICKER date" list, e.g. JPM 2027-02-22) |

The page prints the ledger. Build-time asserts (raise, not warn): buckets are pairwise
disjoint **by `events.id`** and their union equals the set of all ids.

**List scope.** Every shown row carries membership tags from today's CM lists: Portfolio /
Researching / Core coverage / Following-for-Interest. Rows whose ticker is on none of
JP's three lists (P, R, Core) are still shown, tagged "FFI only" or "not on any list
today" — hiding a stored event on a list-membership technicality is an omission without
confirmation. (Today 7 names are FFI-only: CENTA, CHWY, FRPT, LULU, PETS, TSM, WOOF; TSM is the only one with an analyst-day row.)
Membership is *as of the build date*, stated on the page.

### 2.3 Row content + tiering
Per row: date (or imprecise hint), ticker, company, event type, **tier**, list tags,
every stored `event_sources` link labelled by `source_type`, classifier confidence, and
the source excerpt on hover.

- **Confirmed** = status ∈ {confirmed, reminded_30, reminded_7, day_of, completed,
  historical} — these passed the date-grounding gate and the authoritative-source bar.
- **Unconfirmed lead** = discovered / tentative — rendered visibly distinct (muted, "lead"
  badge, confidence shown). Not hidden (some are real — FDX 2026-04-08 has IR + press
  release at 0.82), not promoted. The page says plainly that leads include wrong-issuer
  matches and must be checked against the source link.

### 2.4 Coverage statement (computed, never hard-coded prose about the data)
Headline in the H1 region, before the table: "<n> analyst-day rows recorded · <a> dated
<this year> · <b> dated <last year> · <u> date not yet known". Then the header block, all
numbers derived at build time:
- "Recorded since **<min first_seen_at>**. Discovery looks forward; events before that
  date appear only if a forward search surfaced them, and the classifier is instructed to
  drop past events, so earlier years are effectively unsearched. **<n> analyst-day rows
  recorded for 2025**" (today 0 → "2025: none recorded. Not searched, so this is a gap,
  not an absence").
- Per-year counts labelled "recorded", never "held" or "total".
- Scan scope: "Discovery scans <N> tickers (watchlist Core=Y ∪ FFI). <k> of your
  Portfolio/Researching names and <m> Core-coverage names are **not scanned** — no event
  can appear for them."

### 2.5 Reset / staleness guards (independent of #456's behaviour)
#456: if the artifact is lost, `monday.yml` silently starts a fresh DB from a 14-day scan.
The page must not depend on that being fixed, so it *detects* the symptom:
- `HISTORY_FLOOR = date(2026, 7, 6)` (constant, the measured start of the current chain).
  Dates are compared via `datetime.fromisoformat(first_seen_at).date()` (production writes
  `…+00:00`), never string prefixes. If `min(first_seen_at).date() > HISTORY_FLOOR + 1 day`,
  or the `events` table is empty, render a red top banner: **"Earliest record is <X>,
  expected on or before 2026-07-06: the database was reset (board #456) or its earliest
  rows were deleted. Events recorded before <X> are missing from this page."** The listing
  still renders beneath it, labelled.
- Freshness, printed in the header: (a) artifact age from `provenance.artifact_created_at`
  (the job ran), (b) `skipped_newer_failed` — newer artifacts whose run failed and were
  therefore not used (the informative fact; the selected run's conclusion is always
  `success` by construction, so it is a footer fact, not a gate), (c) `max(last_seen_at)`
  labelled "last time discovery touched a row" (shown, not gated — a quiet healthy week
  legitimately leaves it old). Amber banner when (a) > 8 days (weekly cadence + 1) or (b)
  is non-empty: "newest run <date> failed; showing the last successful DB from <date>".
- Provenance footer: artifact id, created timestamp, run URL + conclusion, build date.

### 2.6 Per-ticker coverage table (the "what is missing" view)
For every ticker on Portfolio ∪ Researching (72 today; Core-coverage-only names are
summarised as a count, not listed — 345 rows would bury the page): recorded-event count in
range, and a state: *scanned, events recorded* / *scanned, none recorded* / *not scanned*.
For the two "none" states, if CM has a CIK, an **EDGAR full-text-search link** for
`"investor day" OR "analyst day"` in that company's filings 2025-01-01 → build date — a
one-click manual check, zero API cost, opened by JP not by the build. No CIK (e.g.
`2715.HK`) → "no CIK in Coverage Manager, no EDGAR check available". A source row with a
NULL `source_url` renders "source recorded without a link" (no broken anchor).

### 2.7 Output + privacy
`exports/analyst_day_listing.html`, added to `.gitignore` so a generated view is not a
second committed copy that drifts (membership is already public via CM, see §1). Self-contained (inline CSS, no JS needed — ~17 rows does
not warrant filters, and no JS means no filter wiring to break silently). Light/dark
tokens per the conference page. `data/ci_snapshot/` also gitignored.

### 2.8 Delivery + currency
- Published as a **private claude.ai Artifact** (Artifact tool, interactive session).
  Precedent: the conference calendar (`cc5ff40a…`) and pairs page.
- Kept current by re-running `python scripts/build_analyst_day_page.py --fetch` and
  re-publishing the same file path. Weekly data changes on Monday only, so a weekly
  interactive refresh suffices. Proposed follow-up (not done here — fleet-root registry):
  register it with `/interactive-jobs` so the refresh is prompted.
- **Bookmark step (owner: the overnight orchestrator / JP, not this worker):** bookmark the
  artifact URL on `#analyst-days` as "Analyst Day Listing" — the conference-calendar
  precedent is a bookmark on `#conferences`. Not done here because this session does not
  post to Slack.

## 3. Rejected alternatives
| Alternative | Why not |
|---|---|
| Build in `monday.yml`, commit HTML / GitHub Pages | Membership is already public via CM, so this is not a leak; rejected because it puts a second, generated copy of the data in git, and because CI cannot publish a claude.ai Artifact (the fleet's standing-page precedent). |
| Read local `data/events.db` | Stale April dev sandbox; renders 3 plausible rows. |
| Run a historical backfill now (EDGAR full-text + Tavily + classifier) | Tavily and Anthropic are metered (overnight rule: no burn); writing backfill rows locally wouldn't reach the CI DB, which lives in an artifact; it needs a past-events classifier mode (rule 1 drops past events) writing `status='historical'`; and it is a pipeline change (Phase 4), not a view. Proposed as the follow-up that actually closes the 2025 gap. |
| Render only confirmed rows | 4 of 10 dated 2026 rows are leads; FDX's is very likely real. Hiding them omits without confirmation; showing them tiered is honest. |
| Hide rows not on P/R/Core | Same omission problem; tag instead. |
| Slack canvas / message | Posting is out of bounds tonight; and a canvas is a second copy that drifts. |
| JS filters like the conference page | 17 rows; filters add a silently-breakable contract for no benefit. |

## 4. Invariants
1. Every shown row is traceable to a stored `events.id` and every one of its `event_sources`.
2. Bucket id-sets are pairwise disjoint and their union is the set of all `events.id` —
   enforced at build, raise on failure.
3. Coverage gaps (history start, 2025 count, unscanned names) are stated with computed
   numbers; nothing on the page implies completeness the data lacks.
4. A DB reset (min first_seen > floor) is visible on the page, not inferred by the reader.
5. Never built from `data/events.db` implicitly; source DB provenance is printed.
6. The HTML is never committed (gitignore pinned by a test).

## 5. Verification
- Build from the real 2026-09-28 artifact; open the HTML (Chrome / text dump) and check:
  row count = 15 shown (10 dated 2026 + 5 undated) against the SQL above; ledger
  reconciles to 55; 2025 sentence reads "none recorded"; unscanned counts match an
  independent set computation.
- Tests (`tests/test_analyst_day_page.py`, no network). Fixtures built through `init_db`
  + `events_repo.upsert_event` with a frozen clock so the shape (`+00:00` timestamps,
  defaults) is production's, plus a dated-but-imprecise row and a row exactly on the floor
  day. Cases: bucket precedence + disjointness (incl. a cancelled 2026 investor day ->
  retired, not shown); reset banner boundary with three rows — floor (no fire), floor+1
  (no fire), floor+2 (fire); the dated-but-imprecise row is a production path, since
  `_supersede_imprecise_null_siblings` retires only NULL-start_date siblings so such a row
  persists beside a later precise one;
  leads render as leads; no fallback to `data/events.db`; artifact selection skips a
  newer failed run (gh calls stubbed); `git -C <REPO> check-ignore` run with
  `cwd=tmp_path` (conftest refuses repo-cwd children) asserting both an ignored and a
  not-ignored path; membership tags from a fixture CM dir; scanned set comes from
  `load_core_watchlist`; amber banner fires on a non-empty `skipped_newer_failed` and on
  artifact age 9 days, not 8. Each guard mutation-checked (break it, watch the test fail).
- Codex review loop until no Critical/High.
- Publish the artifact; report the URL.
