"""One-off builder for src/conference_circuit.json (2026-11-17).

    python scripts/build_conference_circuit.py <specialty.json> <mainstreet.json> <wallst.json>

Merges three web-research result files (dates verified by search on 2026-11-17, each
row carrying its source URL) with the hand-written facts for the 17 series that were
already in src/seed_conferences.py. Kept in the repo as the record of how the file
was produced; the JSON it writes is the source of truth and is edited by hand after.

A series whose NEXT date could not be confirmed gets metadata and no upcoming
instance: the calendar lists it as "next date not published yet". Nothing is guessed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "src" / "conference_circuit.json"
VERIFIED_ON = "2026-11-17"

SCI, INV, TRADE = "Scientific meeting", "Investor conference", "Industry trade show"
MED = "Medical specialty meetings"

# series -> (full_name, organizer, audience, standing, attendance, category, match, extras)
EXISTING = {
    "jpm-healthcare": ("J.P. Morgan Healthcare Conference", "J.P. Morgan",
        "Healthcare company executives and institutional investors",
        "The largest healthcare investor conference of the year",
        "~8,000-9,000 registered and about 525 companies (2026, trade press); invitation-only",
        "Bank conferences", ["J.P. Morgan", "JPMorgan", "JP Morgan"], {"post_summary": True}),
    "asco": ("American Society of Clinical Oncology (ASCO) Annual Meeting",
        "American Society of Clinical Oncology", "Cancer doctors (oncologists)",
        "The primary annual meeting for oncology worldwide; ESMO is the European counterpart",
        "~40,000 (2026, pre-meeting figure)", MED, [], {}),
    "asco-gi": ("ASCO Gastrointestinal Cancers Symposium",
        "American Society of Clinical Oncology",
        "Oncologists who treat digestive-tract cancers (colon, stomach, pancreas, liver)",
        "Subspecialty meeting within oncology; one of several ASCO-run symposia",
        "~5,000 (2026, secondary source)", MED, [], {}),
    "asco-gu": ("ASCO Genitourinary Cancers Symposium",
        "American Society of Clinical Oncology",
        "Oncologists and urologists who treat prostate, bladder and kidney cancers",
        "Subspecialty meeting within oncology; one of several ASCO-run symposia",
        "~6,000 (2025)", MED, [], {}),
    "aacr": ("American Association for Cancer Research (AACR) Annual Meeting",
        "American Association for Cancer Research",
        "Cancer researchers (laboratory and early clinical)",
        "The primary cancer-RESEARCH meeting; 1 of 3 big oncology meetings (with ASCO and ESMO)",
        "~23,000 registrants (2026)", MED, [], {}),
    "esmo": ("European Society for Medical Oncology (ESMO) Congress",
        "European Society for Medical Oncology", "Cancer doctors (oncologists), mainly European",
        "1 of 2 primary global oncology meetings (the other is ASCO)",
        "~37,000 (2025)", MED, [], {}),
    "obesityweek": ("ObesityWeek (annual meeting of The Obesity Society)", "The Obesity Society",
        "Obesity-medicine doctors and obesity researchers",
        "The primary US obesity meeting; obesity drug data also features at ADA and EASD",
        "~4,300 (2026, organizer's pre-event figure)", MED, [], {}),
    "aha": ("American Heart Association (AHA) Scientific Sessions", "American Heart Association",
        "Heart doctors (cardiologists) and cardiovascular researchers",
        "1 of 3 primary global cardiology meetings (with ACC in the spring and ESC in late summer)",
        "~10,000-12,700 (2025)", MED, [], {}),
    "acc": ("American College of Cardiology (ACC) Annual Scientific Session",
        "American College of Cardiology", "Heart doctors (cardiologists)",
        "1 of 3 primary global cardiology meetings (with AHA in the fall and ESC in late summer)",
        "~17,000 (2019 figure; no recent count found)", MED, [], {}),
    "rsna": ("Radiological Society of North America (RSNA) Annual Meeting",
        "Radiological Society of North America",
        "Radiologists and medical-imaging companies",
        "The primary annual meeting for radiology and the largest medical-imaging trade show",
        "~38,000 registrants (2025)", MED, [], {}),
    "ash": ("American Society of Hematology (ASH) Annual Meeting",
        "American Society of Hematology",
        "Blood doctors (hematologists), including blood-cancer specialists",
        "The primary global hematology meeting; EHA is the smaller European one",
        "~30,000 (2025)", MED, [], {}),
    "sabcs": ("San Antonio Breast Cancer Symposium (SABCS)",
        "UT Health San Antonio and the American Association for Cancer Research",
        "Breast-cancer doctors and researchers",
        "Disease-specific: the primary global breast-cancer meeting",
        "~11,000 (2025)", MED, [], {}),
    "tct": ("Transcatheter Cardiovascular Therapeutics (TCT)", "Cardiovascular Research Foundation",
        "Interventional cardiologists (catheter-based heart procedures) and device companies",
        "Subspecialty meeting: the primary US interventional-cardiology meeting (EuroPCR is the European one)",
        "~10,000-12,000 (third-party listings)", MED, [], {}),
    "ada": ("American Diabetes Association (ADA) Scientific Sessions", "American Diabetes Association",
        "Diabetes doctors and researchers",
        "1 of 2 primary global diabetes meetings (the other is EASD)",
        "~12,000 expected (2026)", MED, [], {}),
    "agbt": ("Advances in Genome Biology and Technology (AGBT) General Meeting", "AGBT",
        "Genomics researchers and DNA-sequencing tool companies",
        "The main launch venue for sequencing technology; small and attendance-capped",
        "~1,000 (a cap, per a third-party listing)", MED, [], {}),
    "himss": ("HIMSS Global Health Conference",
        "Healthcare Information and Management Systems Society (HIMSS)",
        "Hospital IT leaders and health-IT vendors",
        "The largest health-IT trade show; ViVE and HLTH are the newer rivals",
        "~24,000 (2026)", "Health tech", [], {}),
    "advamed": ("The MedTech Conference", "AdvaMed (the US medical-device trade association)",
        "Medical-device company executives, regulators and suppliers",
        "The primary annual meeting of the US medical-device industry",
        "~4,400 (2026, trade press)", "Medtech industry", [], {}),
}

SHORT = {
    "asn": "ASN Kidney Week", "acog": "ACOG", "aua": "AUA", "aaos": "AAOS", "nass": "NASS",
    "aan": "AAN", "aaic": "AAIC", "ctad": "CTAD", "aao": "AAO", "arvo": "ARVO",
    "asrs": "ASRS", "acr-rheum": "ACR Convergence", "ddw": "DDW",
    "aasld": "AASLD Liver Meeting", "ats": "ATS", "chest": "CHEST", "endo": "ENDO",
    "easd": "EASD", "esc": "ESC", "hrs": "HRS", "aad": "AAD", "apa": "APA",
    "idweek": "IDWeek", "aaaai": "AAAAI", "acs": "ACS Clinical Congress",
    "asa-anes": "ANESTHESIOLOGY", "acep": "ACEP", "attd": "ATTD", "astro": "ASTRO",
    "wclc": "WCLC", "eha": "EHA", "sleep": "SLEEP",
    "ahca-ncal": "AHCA/NCAL", "leadingage": "LeadingAge", "nic-fall": "NIC Fall",
    "nic-spring": "NIC Spring", "argentum": "Argentum", "care-at-home": "Care at Home",
    "adlm": "ADLM", "war-college": "Executive War College", "acla": "ACLA", "amp": "AMP",
    "nacds-annual": "NACDS Annual", "nacds-tse": "NACDS Total Store Expo", "ncpa": "NCPA",
    "ashp-midyear": "ASHP Midyear", "amcp": "AMCP", "asembia": "Asembia AXS",
    "apha": "APhA", "hda": "HDA DMC", "aha-hospitals": "AHA Hospitals",
    "ache": "ACHE Congress", "hfma": "HFMA", "mgma": "MGMA", "asca": "ASCA",
    "ahip": "AHIP", "hlth": "HLTH", "vive": "ViVE", "beckers": "Becker's",
    "chicago-midwinter": "Chicago Midwinter", "vmx": "VMX", "wvc": "WVC",
}
# bank series -> (short label, phrases that identify it in a company press release)
WALL = {
    # Both Jefferies conferences are announced as "Jefferies Global Healthcare Conference";
    # the dates tell London (November) from New York (June).
    "jefferies-london": ("Jefferies London", ["Jefferies"]),
    "stifel-hc": ("Stifel Healthcare", ["Stifel"]),
    "ubs-hc": ("UBS Healthcare", ["UBS"]),
    "wolfe-hc": ("Wolfe Healthcare", ["Wolfe"]),
    "stephens-nashville": ("Stephens", ["Stephens"]),
    "evercore-hconx": ("Evercore Healthcare", ["Evercore"]),
    "piper-hc": ("Piper Sandler Healthcare", ["Piper Sandler"]),
    "citi-hc": ("Citi Healthcare", ["Citi"]),
    "guggenheim-biotech": ("Guggenheim Biotech", ["Guggenheim"]),
    "oppenheimer-hc": ("Oppenheimer Healthcare", ["Oppenheimer"]),
    "td-cowen-hc": ("TD Cowen Health Care", ["TD Cowen", "Cowen"]),
    "leerink-hc": ("Leerink Healthcare", ["Leerink"]),
    "barclays-hc": ("Barclays Healthcare", ["Barclays"]),
    "raymond-james": ("Raymond James", ["Raymond James"]),
    "keybanc-hc": ("KeyBanc Healthcare", ["KeyBanc"]),
    "canaccord-msk": ("Canaccord Musculoskeletal", ["Musculoskeletal"]),
    "needham-hc": ("Needham Healthcare", ["Needham"]),
    "bofa-hc": ("BofA Health Care", ["BofA", "Bank of America"]),
    "rbc-hc": ("RBC Healthcare", ["RBC"]),
    "goldman-hc": ("Goldman Sachs Healthcare", ["Goldman Sachs"]),
    "jefferies-nyc": ("Jefferies New York", ["Jefferies"]),
    "william-blair": ("William Blair Growth", ["William Blair"]),
    "truist-medtech": ("Truist MedTech", ["Truist"]),
    "canaccord-growth": ("Canaccord Growth", ["Canaccord"]),
    "ms-hc": ("Morgan Stanley Healthcare", ["Morgan Stanley"]),
    "wells-hc": ("Wells Fargo Healthcare", ["Wells Fargo"]),
    "baird-hc": ("Baird Healthcare", ["Baird"]),
    "cantor-hc": ("Cantor Healthcare", ["Cantor"]),
    "hcw": ("H.C. Wainwright", ["Wainwright"]),
}
SKIP = {"ada-smilecon"}          # paused by the ADA after 2025: no meeting to track
NULL_LOCATION = {"mgma"}         # organizer says Philadelphia, an aggregator says San Antonio


def _attendance(s: dict):
    a = s.get("attendance")
    if not a:
        return None
    return a if "attend" in a.lower() or "compan" in a.lower() else a


def _instances(s: dict, short: str, kind: str, why: str) -> list[dict]:
    out = []
    for key in ("last", "next"):
        inst = s.get(key)
        if not inst or not (inst.get("start") and inst.get("end")):
            continue
        year = inst["start"][:4]
        loc = None if s["slug"] in NULL_LOCATION else inst.get("location")
        out.append({
            "name": f"{s['full_name']} {year}", "short_name": f"{short} {year}",
            "series": s["slug"], "start": inst["start"], "end": inst["end"],
            "location": loc, "url": s.get("url") or inst.get("source"),
            "verified_on": VERIFIED_ON, "why": why, "kind": kind,
            "date_source": inst.get("source"),
        })
    return out


def main(argv) -> int:
    spec, main_st, wall = (json.loads(Path(p).read_text(encoding="utf-8")) for p in argv[1:4])
    series: dict[str, dict] = {}
    instances: list[dict] = []

    for slug, (full, org, aud, stand, att, cat, match, extra) in EXISTING.items():
        series[slug] = {"full_name": full, "organizer": org, "audience": aud,
                        "standing": stand, "attendance": att, "category": cat,
                        "match": match, **extra}

    for s in spec["series"]:
        slug = s["slug"]
        series[slug] = {"full_name": s["full_name"], "organizer": s["organizer"],
                        "audience": s["audience"], "standing": s["standing"],
                        "attendance": _attendance(s), "category": MED, "match": []}
        instances += _instances(s, SHORT[slug], SCI, s["standing"])

    for s in main_st["series"]:
        slug = s["slug"]
        if slug in SKIP:
            continue
        series[slug] = {"full_name": s["full_name"], "organizer": s["organizer"],
                        "audience": s["audience"], "standing": s["standing"],
                        "attendance": _attendance(s), "category": s["category"], "match": []}
        instances += _instances(s, SHORT[slug], TRADE, s["standing"])

    for s in wall["series"]:
        slug = s["slug"]
        if slug == "jpm-healthcare":       # already carried, with its own instances
            continue
        bank = s["bank"]
        series[slug] = {"full_name": s["full_name"], "organizer": bank,
                        "audience": f"{s['focus']} companies and institutional investors",
                        "standing": s["standing"], "attendance": _attendance(s),
                        "category": "Bank conferences", "match": WALL[slug][1]}
        instances += _instances(s, WALL[slug][0], INV, s["standing"])

    for m in series.values():            # sentence case, not shouting
        m["standing"] = m["standing"].replace("THE ", "The ", 1) if m["standing"].startswith("THE ") else m["standing"]
        m["standing"] = m["standing"].replace(": THE ", ": the ")
    for i in instances:
        i["why"] = series[i["series"]]["standing"]
    names = [i["name"] for i in instances]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SystemExit(f"duplicate instance names: {sorted(dupes)}")
    OUT.write_text(json.dumps({"built_on": VERIFIED_ON, "series": series,
                               "instances": sorted(instances, key=lambda i: i["start"])},
                              indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(series)} series, {len(instances)} instances -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
