# 2-year analyst-day backfill — run of 2026-11-14

**Outcome:** 61 analyst / investor / R&D / capital markets days held between 2024-11-14 and
2026-11-14 were found on EDGAR and stored as `status='historical'` in `data/backfill_2yr.db`.
`exports/backfill_last2yr.json` lists them.

**Two things are not as specified — read these first.**

1. ⚠ **The export is NOT merged with the live system yet.** Every one of its 61 items has
   `origin: "backfill"`. The live rows come from the CI database artifact, and the download
   failed: `gh` on this machine has an invalid token.
   - Exact error: `HTTP 401: Requires authentication (.../actions/artifacts/14076459272/zip)`;
     `gh auth status` → `The token in default is invalid.`
   - I did not use any other credential to get around it.
   - Fix: `gh auth login -h github.com`, then the last command in "Commands" below. It
     rewrites the export in place, merged, live row preferred on a clash.
2. ⛔ **The failed fetch deleted the previous local snapshot.** `fetch_snapshot` in
   `scripts/build_analyst_day_page.py` unlinks `data/ci_snapshot/events.db` *before* it
   downloads. The download then failed, so the old copy (artifact of 2026-09-28, fetched
   2026-10-05) is gone. `data/ci_snapshot/provenance.json` still describes it.
   - Nothing in production is affected: the artifact itself is on GitHub, and the next
     successful `--fetch` restores a newer copy (the newest successful run is 2026-11-13).
   - Until then `scripts/build_analyst_day_page.py` without `--fetch` exits 2.
   - The old file may also be in Dropbox's deleted files. It was not in the local Dropbox cache.
   - Not fixed here (out of scope): download to a temporary name, then replace.

## Counts

Events listed in `exports/backfill_last2yr.json` (61 events, 55 companies):

| Held in | Investor Day | Analyst Day | R&D Day | Capital Markets Day | Total |
|---|---|---|---|---|---|
| 2024 (from 2024-11-14) | 5 | 2 | 2 | 0 | **9** |
| 2025 | 20 | 5 | 2 | 2 | **29** |
| 2026 (to 2026-11-14) | 20 | 1 | 0 | 2 | **23** |
| **Total** | **45** | **8** | **4** | **4** | **61** |

Funnel, EDGAR to stored:

| Step | Count |
|---|---|
| Scan set (Coverage Manager `analyst_days` scope, production loader) | 368 names |
| …with a CIK, so searchable on EDGAR | 310 |
| …with no CIK, not searched at all | 58 |
| Filing window swept | 2024-09-01 → 2026-11-14, 27 monthly searches, 0 failed |
| 8-K / 6-K documents EDGAR returned for the trigger phrases | 7,862 |
| Filings by scan-set filers | 465 |
| EDGAR hits (filings with a usable excerpt) | 454, across 117 names |
| **Hits classified** (past-events mode, 134 classifier calls, 0 errors) | **454** |
| Events the classifier extracted | 107 |
| – no precise date ("May 2025", "later this year") — not stored | 23 |
| – dated before 2024-11-14 — not stored | 7 |
| – dated after 2026-11-14 (upcoming) — not stored | 6 |
| – in the window | 71 (70 distinct rows; 1 was a second filing for the same event) |
| **Stored `historical`** | **62**, then 1 retired by review → **61** |
| Stored `discovered` (in window, missed a gate, not in the export) | 8 |

## Held back, and one row I retired

8 in-window events are in the database as `discovered` and are **not** in the export:

| Ticker | Type | Date | Why held |
|---|---|---|---|
| HLN | Capital Markets Day | 2025-05-01 | Release says "today"; the date is not in the excerpt |
| SNN | Capital Markets Day | 2025-12-08 | Same ("will today present"). Its 2025-12-11 New York session IS listed |
| HOOD | Investor Day | 2024-12-04 | Same, and confidence 0.82 |
| RVTY | Investor Day | 2024-11-21 | "Thursday, November 21st" — year only implied by the filing date |
| ONC | R&D Day | 2025-06-26 | "June 26" — year only implied |
| RPRX | Investor Day | 2025-09-11 | "September 11th" — year only implied |
| MRNA | R&D Day ("Science Day") | 2026-06-25 | "June 25" — year only implied |
| SPCX | Investor Day | 2026-04-22 | Confidence 0.72: a deck date in an underwriting schedule |

- The first 7 look real. They fail the date-grounding gate exactly as a weekly row would.
  The gate was applied unchanged, as instructed. Promoting them is a decision for you.
- **ZBH "Investor Day 2025-01-28" was a false positive that passed every gate.** The cited
  exhibit is the Paragon 28 acquisition deck dated January 28, 2025; "2024 Investor Day" is
  leftover slide-template text. I retired the row (`status='cancelled'`, reason in
  `events.notes`), so it is not in the export. This is the only row I changed by hand.
- I read the classifier's reason for all 70 rows. I did not open the other 61 filings.

## Known gaps and blind spots

- **EDGAR only, by instruction.** An event announced only on an IR calendar or in an unfiled
  press release is invisible. Examples the sweep shows it missed: Humana's 2025 Investor Day
  (only "May 2025" appears in a filing), Insulet's 2025 day ("rescheduled from June 5").
  193 of the 310 searchable names had no trigger-phrase filing at all in 26 months.
- **58 names have no CIK** (mostly foreign listings: Roche, Lonza, CSL, Straumann, Sartorius,
  the Japanese and Nordic names). Their capital markets days are not in this file. The full
  list is in `data/backfill_run_stats.json` → `scan_set_no_cik`.
- **Today's scan set, applied to the past.** A name that left the lists before 2026-11-14 is
  not swept.
- **"Historical" means the scheduled date has passed, not that EDGAR confirmed it took place.**
  For 32 of 61 the cited filing was made on or after the event day; the other 29 cite an
  announcement made beforehand. A postponement that was never filed would not be caught.
- **Year-less and "today" dates are held back** (7 of the 8 above). This is the gate working
  as designed, and it costs about 10% of real events in a historical pass.
- **23 events had no precise date** and were dropped, not stored. Several are real days whose
  date never appeared in a filing (HUM, PODD, SOLV, MDT, BSX, ALC, ADSK, SGRY, RMD).
- **Trigger phrases only.** An event called an "Investor Conference" (UnitedHealth's name
  for it) or an "investor update" does not match the search.
- **Same event, two types** is possible in principle (the key is ticker + type + date). None
  occurred in this run.
- **Spin-off days sit under the parent's ticker:** SPGI 2026-05-12 is the Mobility Global
  Investor Day; FDX 2026-04-08 is FedEx Freight's.
- **Conferences** are out of scope and the past-events prompt does not extract them.
- **6 upcoming events were seen and deliberately not stored** (outside the window; they belong
  to the weekly run): ARE 2026-12-02, IQV 2026-12-01, ROIV 2026-12-09, BDX 2026-12-10,
  HUM 2026-12-10 (investor update), DGX 2027-03-25. Worth checking these are in the live DB
  once the snapshot can be fetched.

## What failed

| What | Detail |
|---|---|
| Live snapshot fetch | `gh` token invalid → HTTP 401. Blocker 1 above. Not worked around |
| Old snapshot deleted | Side effect of that failed fetch. Blocker 2 above |
| First EDGAR sweep stopped at 20 of 27 months | Hit the 10-minute limit of my shell, not an EDGAR error. Re-run resumed from the cache and finished the other 7; no month was fetched twice |
| EDGAR errors, document fetch errors, classifier errors | 0 |

## What changed in the repo (nothing committed, nothing pushed)

| File | Change |
|---|---|
| `src/discovery/classify.py` | `classify_ticker(past_events=True)` selects a separate past-events prompt and shows the whole excerpt. Default path and the weekly prompt are unchanged |
| `src/state/events_repo.py` | `upsert_event(historical=True)`: same gates as `confirmed`, date must be past, result is `historical` or `discovered`, never `confirmed` or `tentative` |
| `src/discovery/date_grounding.py` | `grounded_in_cited_source` — the weekly gate as a function the backfill can call without importing the CLI |
| `src/backfill.py` (new) | The runner: monthly EDGAR sweep, classify, store. Resumable. Refuses `data/events.db`, the CI snapshot, and GitHub Actions |
| `scripts/export_backfill.py` (new) | Builds the export; `--fetch` reuses the listing page's snapshot fetch |
| `tests/test_backfill.py` (new) | 30 tests |

- **Tests:** `python -m pytest tests/ -q` → **344 passed** (314 before, 30 new).
- **No fan-out:** the backfill process loads no Slack / Calendar / TickTick / email / health
  module (asserted on a fresh interpreter), and a `historical` row is selected by none of the
  fan-out, reminder, digest or export queries (asserted, with a `confirmed` control).
- **Production untouched:** `data/events.db` unmodified (still dated 2026-08-12); no workflow
  file touched; no Slack, Calendar, TickTick or email call made.
- **Not ignored by git:** `data/backfill_2yr.db`, `data/backfill_edgar_hits.json` (549 KB),
  `data/backfill_classified.jsonl`, `data/backfill_run_stats.json` and the export are
  untracked. I left `.gitignore` alone.
- **Outside the repo:** one line in the fleet `SHIPLOG.jsonl` — a review-queue entry
  (`analyst-days-backfill-2yr`), per the workspace instructions. No `shiplog add`: there is
  no commit to cite.
- **Docs not updated:** `CLAUDE.md` / `PROJECT_BRIEF.md` still say Phase 4 is unbuilt and
  that `status='historical'` is never set.

## Scoping decisions I made

- A separate entry point (`python -m src.backfill`) instead of a `--discover` flag, so the
  backfill cannot reach fan-out code. Its `--db` defaults to `data/backfill_2yr.db`.
- Same search phrases, forms (8-K, 6-K) and CIK matching as the weekly run. One search per
  calendar month, because EDGAR will not page past 10,000 results.
- Up to 8 filings per classifier call; same model as the weekly run (`claude-sonnet-4-6`).
- In-window events that miss a gate are stored `discovered`; undated and out-of-window
  events are not stored (they are in `data/backfill_classified.jsonl`).
- Window ends are inclusive. An event dated 2026-11-14 would count; none was found.
- From the live DB the export takes precise-dated, non-retired, non-conference rows in the window.
- `--backfill-only` on the export script exists so this run could deliver the backfill rows
  without the snapshot. It prints a warning and is never the default.
- Classifier spend was not measured (454 excerpts, 134 calls).

## Commands run

```
# volume probe (read-only), then:
python -m src.backfill --db data/backfill_2yr.db --event-start 2024-11-14 --event-end 2026-11-14 --filings-from 2024-09-01 --sweep-only        # stopped at 20/27 months
python -u -m src.backfill --db data/backfill_2yr.db --event-start 2024-11-14 --event-end 2026-11-14 --filings-from 2024-09-01 --sweep-only     # resumed, 27/27

# classifier trial, 6 names, no DB write, separate log:
python -u -m src.backfill --db data/backfill_2yr.db --event-start 2024-11-14 --event-end 2026-11-14 --filings-from 2024-09-01 --dry-run --tickers RVTY BDX MRNA REGN HUM ARGX --classified-log %TEMP%\bf\trial.jsonl --stats %TEMP%\bf\trial_stats.json

# the real run (sweep served from cache; classify + store):
python -u -m src.backfill --db data/backfill_2yr.db --event-start 2024-11-14 --event-end 2026-11-14 --filings-from 2024-09-01

# retire the ZBH false positive (events_repo.retire_event, status 'cancelled', on data/backfill_2yr.db)

# snapshot fetch — FAILED twice with HTTP 401:
python -c "from scripts import build_analyst_day_page as p; p.fetch_snapshot()"
python scripts/export_backfill.py --event-start 2024-11-14 --event-end 2026-11-14 --fetch

# export, backfill rows only:
python scripts/export_backfill.py --event-start 2024-11-14 --event-end 2026-11-14 --backfill-only

python -m pytest tests/ -q                                  # 344 passed
python ../scripts/shiplog.py review --project analyst-days --key analyst-days-backfill-2yr ...
```

**To finish the merge** (after `gh auth login -h github.com`):

```
python scripts/export_backfill.py --event-start 2024-11-14 --event-end 2026-11-14 --fetch
```

## Decisions for you

1. **Re-authenticate `gh` and re-run the export with `--fetch`** — about a minute. Until
   then the export is backfill-only and the listing page cannot rebuild without `--fetch`.
2. **Promote the 7 real-looking held-back events?** Yes = relax the gate for filings where
   the year or "today" resolves from the filing date (a code change), or add them by hand.
   No = they stay out of the export.
3. **Commit the code?** It is tested and inert unless `python -m src.backfill` is run.
4. **A web-search pass for the blind spot?** The 58 no-CIK names and the 193 names with no
   filing hit are where the missing events are. That is the per-name search this run was
   told not to do.

---

# Follow-up, same day: merge finished and a web-search pass added
Done in an interactive session after the run above (JP: "do all the things you recommend").
**Outcome:** `exports/backfill_last2yr.json` now lists **121** events held 2024-11-14 to 2026-11-14 (26 from the live system, 95 from the backfill), across 97 companies.
| Held in | Events |
|---|---|
| 2024 | 14 |
| 2025 | 57 |
| 2026 | 50 |

| Type | Events |
|---|---|
| investor_day | 82 |
| capital_markets_day | 20 |
| rd_day | 10 |
| analyst_day | 9 |

## What changed since the section above
- **Blockers 1 and 2 are closed.** `gh` works in the interactive session; the export was rebuilt with `--fetch` and the local snapshot is back (artifact of 2026-11-13).
- **`fetch_snapshot` no longer deletes the old copy first.** It downloads beside it and swaps (3 tests).
- **The export lists one event per company per date**, so a day held under two types appears once (the live row wins).
- **The code is committed** (`f472df8`).

## The web-search pass (`python -m src.backfill_web`)
Codex on JP's ChatGPT subscription was asked, 8 companies a call, for every such day each scan-set company held in the window. The model proposes; code decides. A row is `historical` only when the cited page was read and states the date, names the event and the company, and is the company's own site, an SEC filing or a press-release wire.

| Step | Count |
|---|---|
| Names asked / answered | 368 / 368 |
| Codex calls | 46 (0 failed, 0 retried) |
| Events proposed | 164 |
| Passed every check | 108 (67 of them were already in the database from EDGAR and gained a second source) |
| Kept as leads (`discovered`) | 56 |
| Database now | 109 historical, 57 discovered, 1 cancelled (was 61 / 8 / 1) |
| Time | 37 min |
| Subscription usage | 28.7M input tokens (25.4M cached), 66k output |

- **6 of the 8 events held back above are now listed** (HLN, HOOD, MRNA, RPRX, RVTY, SNN): the company's own page states the full date. ONC and SPCX are still held.
- **Not opened by a person.** The checks are mechanical. I read the list of 48 additions and did not open the pages.
- **Things that look worth a second look:** BridgeBio has three disease-specific investor days; Eurofins has two regional days from its calendar page; Novo Nordisk's R&D day cites a financial-results page; Lonza 2024-12-12 is typed investor_day on a capital-markets-day page.

## Leads: proposed, not listed (56)

Each failed at least one check. Most are real events under a name the checks do not accept ("Investor Conference", "Pharma Day", "Innovation Webcast").

| Ticker | Type | Date | Why not listed | Source |
|---|---|---|---|---|
| 068270.KS | rd_day | 2025-08-22 | date not on page, event not named | https://www.celltrion.com/ko-kr/company/media-center/press-release/4102 |
| 068270.KS | rd_day | 2026-05-26 | date not on page | https://www.celltrion.com/ko-kr/company/media-center/press-release/4786 |
| 2359.HK | investor_day | 2026-11-06 | not an authoritative source | https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/search/rptid/84 |
| 4519 | rd_day | 2024-12-17 | date not on page, event not named | https://www.chugai-pharm.co.jp/english/ir/reports_downloads/presentations.html |
| 4543.T | investor_day | 2026-06-11 | event not named | https://www.terumo.com/investors/library/presentations |
| 4543.T | rd_day | 2026-10-19 | event not named | https://www.terumo.com/investors/library/presentations |
| 4568.T | rd_day | 2025-12-16 | event not named | https://www.daiichisankyo.com/investors/library/materials/2025/index.html |
| 600276.SS | rd_day | 2025-12-05 | date not on page, event not named, company not named | https://www.hengrui.com/media/detail-889.html |
| 6869.T | rd_day | 2025-03-07 | event not named, not an authoritative source | https://www.irwebcasting.com/20250307/1/fe7e0a0890/media/250307technology_j.pdf |
| 6869.T | rd_day | 2026-03-13 | event not named, not an authoritative source | https://www.irwebcasting.com/20260313/5/5f276507e1/media/presentation02.pdf |
| 7701.T | investor_day | 2025-09-05 | event not named | https://www.shimadzu.com/sites/shimadzu.com/files/ir/od6m/ynjysdi6pebp2y23.pdf |
| 7701.T | investor_day | 2026-09-04 | event not named | https://www.shimadzu.com/sites/shimadzu.com/files/ir/btzv/8glnijm3a5gsu7rw.pdf |
| 8086.T | investor_day | 2026-03-26 | event not named | https://www.nipro.co.jp/en/ir/library/2026.html |
| ADSK | investor_day | 2025-10-07 | date not on page | https://investors.autodesk.com/news-events/investor-day-presentations |
| AFRM | investor_day | 2026-05-12 | event not named | https://investors.affirm.com/news-releases/news-release-details/affirm-host-inve |
| AMZN | analyst_day | 2025-06-03 | not an authoritative source | https://coresight.com/research/revving-up-the-flywheel-insights-from-the-inaugur |
| AMZN | analyst_day | 2025-12-10 | date not on page, not an authoritative source | https://www.linkedin.com/pulse/double-click-amazon-inaugural-grocery-analyst-day |
| AMZN | analyst_day | 2026-04-23 | date not on page, event not named, company not named, not an authoritative source | https://www.marketresearch.com/IDC-v2477/Retail-Innovation-Age-Agentic-AI-452641 |
| AMZN | analyst_day | 2026-05-06 | not an authoritative source | https://coresight.com/industry-events/ |
| BAYN | investor_day | 2025-05-13 | event not named | https://www.bayer.com/en/investors/events/crop-science-investoren-webinar |
| COH.AX | capital_markets_day | 2026-11-13 | page unreadable, date not on page, event not named, company not named, not an authoritative source | https://announcements.asx.com.au/asxpdf/20261113/pdf/076k8mwlwnyss9.pdf |
| CRWD | investor_day | 2025-09-17 | event not named | https://ir.crowdstrike.com/news-releases/news-release-details/crowdstrike-webcas |
| CRWD | investor_day | 2026-09-02 | event not named | https://ir.crowdstrike.com/news-releases/news-release-details/crowdstrike-webcas |
| CTEC LN | capital_markets_day | 2026-04-09 | page unreadable, date not on page, event not named, company not named, not an authoritative source | https://www.convatecgroup.com/investors/results-centre/ |
| DAE | capital_markets_day | 2025-11-19 | not an authoritative source | https://www.marketscreener.com/news/datwyler-datwyler-with-sharpened-growth-stra |
| ERF FP | investor_day | 2025-11-13 | not an authoritative source | https://www.marketscreener.com/news/eurofins-scientific-na-investor-day-lancaste |
| EW | investor_day | 2024-12-04 | date not on page, event not named | https://ir.edwards.com/events-presentations/event-details/2024/2024-Investor-Con |
| EW | investor_day | 2025-12-04 | date not on page, event not named | https://ir.edwards.com/events-presentations/event-details/2025/2025-Investor-Con |
| GILD | analyst_day | 2024-12-10 | event not named | https://investors.gilead.com/events-and-presentations/events/event-details/2024/ |
| HUM | investor_day | 2025-06-16 | event not named | https://www.sec.gov/Archives/edgar/data/49071/000004907125000026/hum-20250602.ht |
| MED | investor_day | 2026-06-04 | event not named | https://medartis.com/en/investors |
| MTD | investor_day | 2026-11-09 | date not on page, event not named | https://investor.mt.com/news/news-details/2026/Mettler-Toledo-International-Inc- |
| NVDA | analyst_day | 2025-03-19 | event not named | https://investor.nvidia.com/events-and-presentations/events-and-presentations/ev |
| NVDA | analyst_day | 2026-03-17 | event not named | https://investor.nvidia.com/events-and-presentations/events-and-presentations/ev |
| NVO | rd_day | 2025-06-22 | event not named | https://www.novonordisk.com/content/dam/nncorp/global/en/investors/pdfs/ada/novo |
| NVO | rd_day | 2026-06-07 | event not named | https://www.novonordisk.com/news-and-media/news-and-ir-materials/news-details.ht |
| NVS | investor_day | 2024-11-21 | event not named | https://prod1.novartis.com/sites/novartis_com/files/meet-novartis-management-202 |
| NVS | investor_day | 2025-11-20 | event not named | https://www.novartis.com/sites/novartis_com/files/meet-novartis-management-2025- |
| ONC | rd_day | 2025-06-26 | date not on page, event not named, company not named | https://ir.beonemedicines.com/news/beone-medicines-showcases-groundbreaking-onco |
| PLSE | analyst_day | 2026-02-06 | event not named | https://investors.pulsebiosciences.com/static-files/5aa3bbb3-0553-4c1b-8ca1-19cd |
| PLSE | analyst_day | 2026-04-25 | event not named | https://investors.pulsebiosciences.com/news-releases/news-release-details/pulse- |
| Q | investor_day | 2025-09-18 | not an authoritative source | https://www.dupont.com/news/qnity-electronics-inc-to-host-2025-investor-day-on-s |
| ROG.SW | investor_day | 2025-05-27 | date not on page, event not named | https://assets.roche.com/f/176343/x/0cd9054dc9/irp250424.pdf |
| ROG.SW | investor_day | 2026-05-12 | date not on page, event not named | https://assets.roche.com/f/176343/x/dea78e334b/irp260129.pdf |
| ROG.SW | rd_day | 2024-11-25 | event not named | https://www.roche.com/investors/events/roche-virtual-ir-digitalization-day |
| ROG.SW | rd_day | 2025-09-22 | page unreadable, date not on page, event not named, company not named, not an authoritative source | https://assets.roche.com/f/176343/x/059c686d27/20250922_pharma-day-2025_vf_onlin |
| ROG.SW | rd_day | 2026-09-28 | page unreadable, date not on page, event not named, company not named, not an authoritative source | https://www.roche.com/investors/updates/inv-update-2026-08-13 |
| SDZ | capital_markets_day | 2026-09-08 | date not on page | https://www.sandoz.com/sandoz-hosts-capital-markets-day-and-unveils-bio100-ambit |
| SPCX | analyst_day | 2026-04-21 | company not named, not an authoritative source | https://www.marketscreener.com/news/spacex-will-host-analyst-day-on-april-21-sou |
| TMO | investor_day | 2026-05-20 | date not on page, not an authoritative source | https://thermofisher2026investorday.q4ir.com/home/default.aspx |
| UCB | capital_markets_day | 2026-06-08 | event not named | https://www.ucb.com/Investors/EULAR-KOL-BEBOLD-Event |
| UNH | investor_day | 2024-12-04 | event not named | https://www.unitedhealthgroup.com/newsroom/2024/2024-11-26-uhg-to-host-2024-inve |
| VTRS | investor_day | 2026-03-19 | event not named | https://investor.viatris.com/events-and-presentations |
| WMT | investor_day | 2025-04-09 | event not named | https://corporate.walmart.com/news/events/walmart-investment-community-meeting-a |
| YPSN | capital_markets_day | 2025-09-29 | page unreadable, date not on page, event not named, company not named, not an authoritative source | https://www.ypsomed.com/en/investors/financial-calendar |
| ZTS | rd_day | 2025-12-02 | event not named | https://investor.zoetis.com/news/news-details/2025/Zoetis-to-Host-Innovation-Web |
