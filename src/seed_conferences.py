"""Seed the industry-conference calendar (board #58).

    python -m src.seed_conferences --dry-run    # print, write nothing
    python -m src.seed_conferences              # upsert into data/events.db

WHY A CURATED SEED AND NOT A SCRAPER. Board #58 asks for a sell-side-sourced calendar,
and that half is genuinely hard — bank conference pages are inconsistent and several are
gated. But the *medical* circuit is stable year to year, published 12-18 months ahead, and
is what actually moves healthcare names. Seeding it delivers the forward calendar now and
turns the scraper into a refinement rather than a prerequisite.

The `conferences` and `conference_presentations` tables already existed here and held ZERO
rows — the schema was designed for exactly this and never populated. `analyst-days`
otherwise tracks conferences COMPANY-anchored ("is ISRG presenting at JPM?"), which cannot
answer "what are the key HC conferences this year".

THIS CONSTANT IS THE SOURCE OF TRUTH, NOT THE TABLE. The DB rows are a pure projection of
`CONFERENCES` below: the seed is idempotent, so edit here and re-run. That also means the
table survives losing `data/events.db` — which matters, because the DB is gitignored and
lives between CI runs as an expiring GitHub Actions artifact. `events` rebuilds from
discovery if that artifact is lost; `conferences` rebuilds ONLY from this file, so the
weekly workflow re-seeds every run.

⚠ DATES ARE EVIDENCE, NOT MEMORY. Every row below carries `date_status`:
  * `verified`   — confirmed by web search on the date in `verified_on`, source in `url`
  * `unconfirmed`— on the circuit and worth tracking, but the date was NOT confirmed, so
                   start/end are stored NULL rather than guessed
A wrong conference date is worse than a missing one: it silently mis-anchors every
catalyst expectation hung off it. Anything not confirmed goes in dateless and says so.

Re-run after checking the unconfirmed rows, or annually as new dates publish.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Optional

from src.state.schema import init_db

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "events.db")

# Sector vocabulary. Healthcare dominates today; tech and consumer are planned.
SECTORS = ("Healthcare", "Technology", "Consumer")


@dataclass(frozen=True)
class Conference:
    """One conference INSTANCE (a specific year's meeting).

    `series` is the stable slug tying instances together across years — `name` is
    UNIQUE on the instance ("J.P. Morgan 45th Annual Healthcare Conference"), so
    JPM 2028 will be a separate row with no intrinsic link to JPM 2027. Rollover
    detection and any cross-year presentation history both key off `series`.

    `host_ticker` is set only when a company OWNS the event. Every healthcare row
    leaves it None — ASCO owns ASCO and hundreds of companies present. The tech
    circuit splits (CES/MWC neutral; WWDC/GTC company-owned) and company-owned
    events still belong in this table: `events.event_type='conference'` is not in
    PUSHABLE_EVENT_TYPES, so routing them there would fan out nowhere and split
    the calendar across two tables for no gain.
    """
    name: str
    short_name: str
    series: str
    start: Optional[str]
    end: Optional[str]
    location: Optional[str]
    url: str
    date_status: str          # "verified" | "unconfirmed"
    verified_on: Optional[str]
    why_it_matters: str
    sector: str = "Healthcare"
    host_ticker: Optional[str] = None


C = Conference

# NOTE ON `name`: it is UNIQUE per INSTANCE, so every recurring meeting carries its
# year in the name ("ASCO Annual Meeting 2026"), and `series` is what ties the
# instances together. The J.P. Morgan rows use their ordinal instead (43rd/44th/45th),
# which is already instance-unique and is how that conference is referred to.
CONFERENCES: list[Conference] = [
    # ---- HELD — backfilled and verified 2026-08-12 by web search ----------------------
    # The seeder was forward-only until then, so the entire 2025 circuit and the first
    # half of 2026 were missing: conferences that had already happened were never
    # recorded anywhere. Held instances are what make `series` useful (year-over-year
    # comparison, rollover detection) and are the history half of the published calendar.
    C("J.P. Morgan 43rd Annual Healthcare Conference", "JPM 2025", "jpm-healthcare",
      "2025-01-13", "2025-01-16", "San Francisco, CA",
      "https://www.jpmorgan.com/about-us/events-conferences/health-care-conference",
      "verified", "2026-08-12",
      "THE healthcare investment event of the year. Guidance resets, M&A announcements "
      "and the year's strategic tone are set here."),
    C("ASCO Gastrointestinal Cancers Symposium 2025", "ASCO GI 2025", "asco-gi",
      "2025-01-23", "2025-01-25", "San Francisco, CA",
      "https://www.asco.org/gi", "verified", "2026-08-12",
      "GI oncology readouts — colorectal, gastric, pancreatic, HCC."),
    C("ASCO Genitourinary Cancers Symposium 2025", "ASCO GU 2025", "asco-gu",
      "2025-02-13", "2025-02-15", "San Francisco, CA",
      "https://www.asco.org/gu", "verified", "2026-08-12",
      "GU oncology — prostate, bladder, renal."),
    C("AACR Annual Meeting 2025", "AACR 2025", "aacr",
      "2025-04-25", "2025-04-30", "Chicago, IL",
      "https://www.aacr.org/professionals/meetings/", "verified", "2026-08-12",
      "Translational/early oncology. Earlier-stage than ASCO — first-in-human and "
      "mechanism data, so it moves small/mid-cap biotech hardest."),
    C("ASCO Annual Meeting 2025", "ASCO 2025", "asco",
      "2025-05-30", "2025-06-03", "Chicago, IL",
      "https://www.asco.org/annual-meeting", "verified", "2026-08-12",
      "The largest oncology meeting. Registrational Phase 3 readouts; the single biggest "
      "catalyst cluster of the year for oncology names."),
    C("ESMO Congress 2025", "ESMO 2025", "esmo",
      "2025-10-17", "2025-10-21", "Berlin, Germany",
      "https://www.esmo.org/meeting-calendar/esmo-congress-2025", "verified", "2026-08-12",
      "Europe's ASCO equivalent. Often carries practice-changing Phase 3 data, and is the "
      "venue for EU-first approvals and readouts."),
    C("ObesityWeek 2025", "ObesityWeek 2025", "obesityweek",
      "2025-11-04", "2025-11-07", "Atlanta, GA",
      "https://obesityweek.org/", "verified", "2026-08-12",
      "Obesity/cardiometabolic. Directly relevant to the GLP-1 complex and its "
      "second-wave competitors."),
    C("AHA Scientific Sessions 2025", "AHA 2025", "aha",
      "2025-11-07", "2025-11-10", "New Orleans, LA",
      "https://professional.heart.org/en/meetings/scientific-sessions", "verified",
      "2026-08-12",
      "Cardiovascular outcomes trials — the venue that has repeatedly re-rated cardio-"
      "metabolic names (GLP-1 CV outcomes, lipid-lowering)."),
    C("RSNA Scientific Assembly and Annual Meeting 2025", "RSNA 2025", "rsna",
      "2025-11-30", "2025-12-04", "Chicago, IL",
      "https://www.rsna.org/annual-meeting", "verified", "2026-08-12",
      "Radiology and imaging — the medtech/imaging product cycle venue (GEHC, SHL, "
      "Philips, Siemens Healthineers) and increasingly the AI-in-imaging showcase."),
    C("ASH 67th Annual Meeting and Exposition", "ASH 2025", "ash",
      "2025-12-06", "2025-12-09", "Orlando, FL",
      "https://www.hematology.org/meetings/annual-meeting", "verified", "2026-08-12",
      "Hematology — heme-onc, cell and gene therapy, sickle cell. The year's last major "
      "biotech catalyst cluster."),
    C("ASCO Gastrointestinal Cancers Symposium 2026", "ASCO GI 2026", "asco-gi",
      "2026-01-08", "2026-01-10", "San Francisco, CA",
      "https://www.asco.org/gi", "verified", "2026-08-12",
      "GI oncology readouts — colorectal, gastric, pancreatic, HCC."),
    C("J.P. Morgan 44th Annual Healthcare Conference", "JPM 2026", "jpm-healthcare",
      "2026-01-12", "2026-01-15", "San Francisco, CA",
      "https://www.jpmorgan.com/about-us/events-conferences/health-care-conference",
      "verified", "2026-08-12",
      "THE healthcare investment event of the year. Guidance resets, M&A announcements "
      "and the year's strategic tone are set here."),
    C("ASCO Genitourinary Cancers Symposium 2026", "ASCO GU 2026", "asco-gu",
      "2026-02-26", "2026-02-28", "San Francisco, CA",
      "https://www.asco.org/gu", "verified", "2026-08-12",
      "GU oncology — prostate, bladder, renal."),
    C("AACR Annual Meeting 2026", "AACR 2026", "aacr",
      "2026-04-17", "2026-04-22", "San Diego, CA",
      "https://www.aacr.org/meeting/aacr-annual-meeting-2026/", "verified", "2026-08-12",
      "Translational/early oncology. Earlier-stage than ASCO — first-in-human and "
      "mechanism data, so it moves small/mid-cap biotech hardest."),
    C("ASCO Annual Meeting 2026", "ASCO 2026", "asco",
      "2026-05-29", "2026-06-02", "Chicago, IL",
      "https://www.asco.org/annual-meeting", "verified", "2026-08-12",
      "The largest oncology meeting. Registrational Phase 3 readouts; the single biggest "
      "catalyst cluster of the year for oncology names."),

    # ---- UPCOMING — verified 2026-08-05 by web search ---------------------------------
    C("ESMO Congress 2026", "ESMO 2026", "esmo",
      "2026-10-23", "2026-10-27", "Madrid, Spain",
      "https://www.esmo.org/meeting-calendar/esmo-congress-2026", "verified", "2026-08-05",
      "Europe's ASCO equivalent. Often carries practice-changing Phase 3 data, and is the "
      "venue for EU-first approvals and readouts."),
    C("AHA Scientific Sessions 2026", "AHA 2026", "aha",
      "2026-11-06", "2026-11-09", "Chicago, IL",
      "https://professional.heart.org/en/meetings/scientific-sessions", "verified",
      "2026-08-05",
      "Cardiovascular outcomes trials — the venue that has repeatedly re-rated cardio-"
      "metabolic names (GLP-1 CV outcomes, lipid-lowering)."),
    C("ObesityWeek 2026", "ObesityWeek 2026", "obesityweek",
      "2026-11-14", "2026-11-17", "Washington, DC",
      "https://obesityweek.org/", "verified", "2026-08-05",
      "Obesity/cardiometabolic. Directly relevant to the GLP-1 complex and its "
      "second-wave competitors."),
    C("RSNA Scientific Assembly and Annual Meeting 2026", "RSNA 2026", "rsna",
      "2026-11-29", "2026-12-03", "Chicago, IL",
      "https://www.rsna.org/annual-meeting", "verified", "2026-08-05",
      "Radiology and imaging — the medtech/imaging product cycle venue (GEHC, SHL, "
      "Philips, Siemens Healthineers) and increasingly the AI-in-imaging showcase."),
    C("ASH Annual Meeting and Exposition 2026", "ASH 2026", "ash",
      "2026-12-12", "2026-12-15", "New Orleans, LA",
      "https://www.hematology.org/meetings/annual-meeting", "verified", "2026-08-05",
      "Hematology — heme-onc, cell and gene therapy, sickle cell. The year's last major "
      "biotech catalyst cluster."),
    C("J.P. Morgan 45th Annual Healthcare Conference", "JPM 2027", "jpm-healthcare",
      "2027-01-11", "2027-01-14", "San Francisco, CA",
      "https://jpmannualhealthcareconference.com/", "verified", "2026-08-05",
      "THE healthcare investment event of the year. ~8,000 attendees, 500+ company "
      "presentations. Invitation-only. Guidance resets, M&A announcements and the year's "
      "strategic tone are set here; the week itself moves the whole sector."),
    C("ASCO Gastrointestinal Cancers Symposium 2027", "ASCO GI 2027", "asco-gi",
      "2027-01-21", "2027-01-23", "San Francisco, CA",
      "https://www.asco.org/calendar", "verified", "2026-08-05",
      "GI oncology readouts — colorectal, gastric, pancreatic, HCC."),
    C("ASCO Genitourinary Cancers Symposium 2027", "ASCO GU 2027", "asco-gu",
      "2027-02-11", "2027-02-13", "San Francisco, CA",
      "https://www.asco.org/calendar", "verified", "2026-08-05",
      "GU oncology — prostate, bladder, renal."),
    C("AACR Annual Meeting 2027", "AACR 2027", "aacr",
      "2027-04-02", "2027-04-07", None,
      "https://onc-rg.com/conferences/aacr-2027/", "verified", "2026-08-05",
      "Translational/early oncology. Earlier-stage than ASCO — first-in-human and "
      "mechanism data, so it moves small/mid-cap biotech hardest."),
    C("ASCO Annual Meeting 2027", "ASCO 2027", "asco",
      "2027-06-04", "2027-06-08", "Chicago, IL",
      "https://www.asco.org/annual-meeting", "verified", "2026-08-05",
      "The largest oncology meeting. Registrational Phase 3 readouts; the single biggest "
      "catalyst cluster of the year for oncology names."),

    # ---- on the circuit, DATE NOT CONFIRMED — stored dateless on purpose --------------
    C("TCT (Transcatheter Cardiovascular Therapeutics)", "TCT", "tct",
      None, None, None,
      "https://www.crf.org/tct", "unconfirmed", None,
      "Interventional cardiology — structural heart and TAVR/mitral device data. Core "
      "medtech catalyst (EW, ABT, MDT, BSX). CONFIRM DATE before relying on it."),
    C("San Antonio Breast Cancer Symposium", "SABCS", "sabcs",
      None, None, "San Antonio, TX",
      "https://www.sabcs.org/", "unconfirmed", None,
      "Breast cancer — ADC and CDK4/6 readouts. CONFIRM DATE."),
    C("ACC Annual Scientific Session", "ACC", "acc",
      None, None, None,
      "https://www.acc.org/", "unconfirmed", None,
      "Cardiology — late-breaking CV trials, spring counterpart to AHA. CONFIRM DATE."),
    C("ADA Scientific Sessions", "ADA", "ada",
      None, None, None,
      "https://professional.diabetes.org/", "unconfirmed", None,
      "Diabetes — the other half of the cardiometabolic/GLP-1 catalyst calendar with "
      "ObesityWeek and EASD. CONFIRM DATE."),
    C("HIMSS Global Health Conference", "HIMSS", "himss",
      None, None, None,
      "https://www.himss.org/", "unconfirmed", None,
      "Health IT and digital health. Relevant to providers/payers and HC IT names rather "
      "than to drug catalysts. CONFIRM DATE."),
    C("AdvaMed MedTech Conference", "AdvaMed", "advamed",
      None, None, None,
      "https://advamed.org/", "unconfirmed", None,
      "The medtech industry's own investor-facing conference. CONFIRM DATE."),
    C("AGBT General Meeting", "AGBT", "agbt",
      None, None, "Marco Island, FL",
      "https://www.agbt.org/", "unconfirmed", None,
      "Genomics/sequencing — the life-science tools product-cycle venue (ILMN, PacBio, "
      "Oxford Nanopore, 10x). CONFIRM DATE."),
]


def rows_for_seed() -> list[tuple]:
    """(short_name, name, series, sector, host_ticker, start, end, loc, url, notes)."""
    out = []
    for c in CONFERENCES:
        note = f"[{c.date_status}"
        if c.verified_on:
            note += f" {c.verified_on}"
        note += f"] {c.why_it_matters}"
        out.append((c.short_name, c.name, c.series, c.sector, c.host_ticker,
                    c.start, c.end, c.location, c.url, note))
    return out


def unconfirmed() -> list[Conference]:
    """Circuit entries with no confirmed date — never written, reported instead.

    The digest imports this so the backlog is visible without anyone manually
    running the seeder. It will grow as tech/consumer are added: those circuits
    publish dates later and less regularly than the medical one.
    """
    return [c for c in CONFERENCES if not (c.start and c.end)]


def seed(db_path: str = DB_PATH, dry_run: bool = False) -> tuple[int, int]:
    """Upsert every DATED conference. Returns (added, updated).

    Schema comes from `init_db` — this module deliberately carries no DDL of its
    own. It used to, and the duplicate definition is exactly what broke the first
    run: `CREATE TABLE IF NOT EXISTS` silently no-ops against the existing table,
    so the local DDL looked authoritative and wasn't, and every undated row then
    failed the real `start_date NOT NULL` and rolled the whole insert back — the
    dry run claimed 17 added and 0 were written.
    """
    conn = init_db(db_path)
    existing = {r[0] for r in conn.execute("SELECT short_name FROM conferences")}
    added = updated = 0
    # Dated rows only. The table requires start_date AND end_date, and an undated
    # conference is a lead to chase, not a calendar entry.
    for (short, name, series, sector, host, start, end, loc, url, note) in rows_for_seed():
        if not (start and end):
            continue
        if short in existing:
            if not dry_run:
                conn.execute(
                    "UPDATE conferences SET name=?,series=?,sector=?,host_ticker=?,"
                    "start_date=?,end_date=?,location=?,url=?,notes=? WHERE short_name=?",
                    (name, series, sector, host, start, end, loc, url, note, short))
            updated += 1
        else:
            if not dry_run:
                conn.execute(
                    "INSERT INTO conferences (name,short_name,series,sector,host_ticker,"
                    "start_date,end_date,location,url,notes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (name, short, series, sector, host, start, end, loc, url, note))
            added += 1
    if not dry_run:
        conn.commit()
    conn.close()
    return added, updated


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args(argv)

    undated = unconfirmed()
    dated = [c for c in CONFERENCES if c.start]
    print(f"{len(CONFERENCES)} conferences: {len(dated)} with VERIFIED dates, "
          f"{len(undated)} on the circuit with dates NOT confirmed\n")
    for c in CONFERENCES:
        when = f"{c.start} -> {c.end}" if c.start else "date UNCONFIRMED"
        host = f" host={c.host_ticker}" if c.host_ticker else ""
        print(f"  {c.short_name:18} {when:26} {(c.location or ''):22} "
              f"{c.sector:11} [{c.date_status}]{host}")
    added, updated = seed(args.db, dry_run=args.dry_run)
    print(f"\n{'DRY RUN - ' if args.dry_run else ''}{added} added, {updated} updated")
    if undated:
        print(f"\n{len(undated)} conference(s) NOT seeded, date unconfirmed: "
              + ", ".join(c.short_name for c in undated))
        print("  On the circuit and worth tracking, but the table requires a date and none "
              "was guessed: a wrong conference date silently mis-anchors every catalyst "
              "hung off it. Confirm, move into the verified block, re-run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
