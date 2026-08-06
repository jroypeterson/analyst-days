"""Seed the healthcare-conference calendar (board #58).

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
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "events.db")

# name, short_name, start, end, location, url, date_status, verified_on, why_it_matters
CONFERENCES = [
    # ---- verified 2026-08-05 by web search -------------------------------------------
    ("J.P. Morgan 45th Annual Healthcare Conference", "JPM 2027",
     "2027-01-11", "2027-01-14", "San Francisco, CA",
     "https://jpmannualhealthcareconference.com/", "verified", "2026-08-05",
     "THE healthcare investment event of the year. ~8,000 attendees, 500+ company "
     "presentations. Invitation-only. Guidance resets, M&A announcements and the year's "
     "strategic tone are set here; the week itself moves the whole sector."),
    ("ASCO Gastrointestinal Cancers Symposium", "ASCO GI 2027",
     "2027-01-21", "2027-01-23", "San Francisco, CA",
     "https://www.asco.org/calendar", "verified", "2026-08-05",
     "GI oncology readouts — colorectal, gastric, pancreatic, HCC."),
    ("ASCO Genitourinary Cancers Symposium", "ASCO GU 2027",
     "2027-02-11", "2027-02-13", "San Francisco, CA",
     "https://www.asco.org/calendar", "verified", "2026-08-05",
     "GU oncology — prostate, bladder, renal."),
    ("AACR Annual Meeting", "AACR 2027",
     "2027-04-02", "2027-04-07", None,
     "https://onc-rg.com/conferences/aacr-2027/", "verified", "2026-08-05",
     "Translational/early oncology. Earlier-stage than ASCO — first-in-human and "
     "mechanism data, so it moves small/mid-cap biotech hardest."),
    ("ASCO Annual Meeting", "ASCO 2027",
     "2027-06-04", "2027-06-08", "Chicago, IL",
     "https://www.asco.org/annual-meeting", "verified", "2026-08-05",
     "The largest oncology meeting. Registrational Phase 3 readouts; the single biggest "
     "catalyst cluster of the year for oncology names."),
    ("ESMO Congress", "ESMO 2026",
     "2026-10-23", "2026-10-27", "Madrid, Spain",
     "https://www.esmo.org/meeting-calendar/esmo-congress-2026", "verified", "2026-08-05",
     "Europe's ASCO equivalent. Often carries practice-changing Phase 3 data, and is the "
     "venue for EU-first approvals and readouts."),
    ("AHA Scientific Sessions", "AHA 2026",
     "2026-11-06", "2026-11-09", "Chicago, IL",
     "https://professional.heart.org/en/meetings/scientific-sessions", "verified",
     "2026-08-05",
     "Cardiovascular outcomes trials — the venue that has repeatedly re-rated cardio-"
     "metabolic names (GLP-1 CV outcomes, lipid-lowering)."),
    ("ObesityWeek", "ObesityWeek 2026",
     "2026-11-14", "2026-11-17", "Washington, DC",
     "https://obesityweek.org/", "verified", "2026-08-05",
     "Obesity/cardiometabolic. Directly relevant to the GLP-1 complex and its "
     "second-wave competitors."),
    ("RSNA Scientific Assembly and Annual Meeting", "RSNA 2026",
     "2026-11-29", "2026-12-03", "Chicago, IL",
     "https://www.rsna.org/annual-meeting", "verified", "2026-08-05",
     "Radiology and imaging — the medtech/imaging product cycle venue (GEHC, SHL, "
     "Philips, Siemens Healthineers) and increasingly the AI-in-imaging showcase."),
    ("ASH Annual Meeting and Exposition", "ASH 2026",
     "2026-12-12", "2026-12-15", "New Orleans, LA",
     "https://www.hematology.org/meetings/annual-meeting", "verified", "2026-08-05",
     "Hematology — heme-onc, cell and gene therapy, sickle cell. The year's last major "
     "biotech catalyst cluster."),

    # ---- on the circuit, DATE NOT CONFIRMED — stored dateless on purpose --------------
    ("TCT (Transcatheter Cardiovascular Therapeutics)", "TCT", None, None, None,
     "https://www.crf.org/tct", "unconfirmed", None,
     "Interventional cardiology — structural heart and TAVR/mitral device data. Core "
     "medtech catalyst (EW, ABT, MDT, BSX). CONFIRM DATE before relying on it."),
    ("San Antonio Breast Cancer Symposium", "SABCS", None, None, "San Antonio, TX",
     "https://www.sabcs.org/", "unconfirmed", None,
     "Breast cancer — ADC and CDK4/6 readouts. CONFIRM DATE."),
    ("ACC Annual Scientific Session", "ACC", None, None, None,
     "https://www.acc.org/", "unconfirmed", None,
     "Cardiology — late-breaking CV trials, spring counterpart to AHA. CONFIRM DATE."),
    ("ADA Scientific Sessions", "ADA", None, None, None,
     "https://professional.diabetes.org/", "unconfirmed", None,
     "Diabetes — the other half of the cardiometabolic/GLP-1 catalyst calendar with "
     "ObesityWeek and EASD. CONFIRM DATE."),
    ("HIMSS Global Health Conference", "HIMSS", None, None, None,
     "https://www.himss.org/", "unconfirmed", None,
     "Health IT and digital health. Relevant to providers/payers and HC IT names rather "
     "than to drug catalysts. CONFIRM DATE."),
    ("AdvaMed MedTech Conference", "AdvaMed", None, None, None,
     "https://advamed.org/", "unconfirmed", None,
     "The medtech industry's own investor-facing conference. CONFIRM DATE."),
    ("AGBT General Meeting", "AGBT", None, None, "Marco Island, FL",
     "https://www.agbt.org/", "unconfirmed", None,
     "Genomics/sequencing — the life-science tools product-cycle venue (ILMN, PacBio, "
     "Oxford Nanopore, 10x). CONFIRM DATE."),
]

# The live table predates this seeder and is STRICTER than a permissive CREATE would be:
#   name NOT NULL UNIQUE, start_date NOT NULL, end_date NOT NULL
# That is a deliberate design call — a conference row without a date is not a calendar
# entry — and it is respected rather than relaxed. The consequence is that undated circuit
# entries CANNOT be stored, so they are reported as work-to-do instead of written as
# half-rows. (`CREATE TABLE IF NOT EXISTS` silently no-ops against an existing table, so
# the first version of this seeder appeared to define the schema and did not: every
# undated row failed the real NOT NULL and rolled the whole insert back — 17 rows claimed
# added in a dry run, 0 actually written.)
DDL = """
CREATE TABLE IF NOT EXISTS conferences (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL UNIQUE,
  short_name  TEXT,
  start_date  TEXT NOT NULL,
  end_date    TEXT NOT NULL,
  location    TEXT,
  url         TEXT,
  notes       TEXT
)"""


def rows_for_seed() -> list[tuple]:
    """(short_name, name, start, end, location, url, notes) ready to upsert."""
    out = []
    for (name, short, start, end, loc, url, status, verified_on, why) in CONFERENCES:
        note = f"[{status}"
        if verified_on:
            note += f" {verified_on}"
        note += f"] {why}"
        out.append((short, name, start, end, loc, url, note))
    return out


def seed(db_path: str = DB_PATH, dry_run: bool = False) -> int:
    conn = sqlite3.connect(db_path)
    conn.execute(DDL)
    existing = {r[0] for r in conn.execute("SELECT short_name FROM conferences")}
    added = updated = 0
    # Dated rows only. See the DDL note: the table requires start_date AND end_date, and
    # an undated conference is a lead to chase, not a calendar entry.
    for short, name, start, end, loc, url, note in rows_for_seed():
        if not (start and end):
            continue
        if short in existing:
            if not dry_run:
                conn.execute(
                    "UPDATE conferences SET name=?,start_date=?,end_date=?,location=?,"
                    "url=?,notes=? WHERE short_name=?",
                    (name, start, end, loc, url, note, short))
            updated += 1
        else:
            if not dry_run:
                conn.execute(
                    "INSERT INTO conferences (name,short_name,start_date,end_date,"
                    "location,url,notes) VALUES (?,?,?,?,?,?,?)",
                    (name, short, start, end, loc, url, note))
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

    dated = [c for c in CONFERENCES if c[2]]
    undated = [c for c in CONFERENCES if not c[2]]
    print(f"{len(CONFERENCES)} conferences: {len(dated)} with VERIFIED dates, "
          f"{len(undated)} on the circuit with dates NOT confirmed\n")
    for (name, short, start, end, loc, url, status, _v, _w) in CONFERENCES:
        when = f"{start} -> {end}" if start else "date UNCONFIRMED"
        print(f"  {short:18} {when:26} {loc or '':22} [{status}]")
    added, updated = seed(args.db, dry_run=args.dry_run)
    print(f"\n{'DRY RUN — ' if args.dry_run else ''}{added} added, {updated} updated")
    if undated:
        print(f"\n{len(undated)} conference(s) NOT seeded, date unconfirmed: "
              + ", ".join(c[1] for c in undated))
        print("  On the circuit and worth tracking, but the table requires a date and none "
              "was guessed: a wrong conference date silently mis-anchors every catalyst "
              "hung off it. Confirm, move into the verified block, re-run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
