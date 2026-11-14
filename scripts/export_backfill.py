"""Merge the backfilled history with what the live system already knows.

    python scripts/export_backfill.py --fetch \
        --event-start 2024-11-14 --event-end 2026-11-14

Writes `exports/backfill_last2yr.json`: ONE array of every analyst / investor / R&D /
capital markets day held inside the window, from two places:

* `origin: "backfill"` -- `status='historical'` rows of `data/backfill_2yr.db`
  (`python -m src.backfill`). Rows the backfill stored as `discovered` missed a gate
  (date not grounded, or confidence under the bar) and are NOT listed.
* `origin: "live"` -- rows of the production database with a precise date in the
  window. Read from the fetched CI snapshot, never `data/events.db`: the real database
  exists only as the `analyst-days-db` GitHub Actions artifact, and the local file is
  a sandbox (see `scripts/build_analyst_day_page.py`, whose fetch this reuses).
  Retired rows (`cancelled` / `superseded`) and conference appearances are left out.

Deduplicated on (ticker, event_type, start_date); the live row wins a clash. Read-only
on both databases. Without a snapshot it exits 2 rather than publish the backfill
alone under a name that says "merged" -- unless `--backfill-only` says to, in which
case it warns and every item carries `origin: "backfill"`.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import build_analyst_day_page as page  # noqa: E402

DEFAULT_BACKFILL_DB = REPO / "data" / "backfill_2yr.db"
DEFAULT_OUT = REPO / "exports" / "backfill_last2yr.json"


def _rows(db_path: Path) -> list[dict]:
    """events + their sources, opened read-only so a wrong path cannot create a DB."""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY start_date, ticker")]
        src: dict[int, list[dict]] = {}
        for s in conn.execute("SELECT * FROM event_sources ORDER BY retrieved_at, id"):
            src.setdefault(s["event_id"], []).append(dict(s))
    finally:
        conn.close()
    for r in rows:
        r["sources"] = src.get(r["id"], [])
    return rows


def _item(r: dict, origin: str) -> dict:
    sources = []
    for s in r["sources"]:
        url = page._safe_url(s.get("source_url"))      # http(s) only, as on the page
        if url:
            sources.append({"label": page.SOURCE_LABEL.get(s["source_type"], s["source_type"]),
                            "url": url})
    return {
        "ticker": r["ticker"], "company_name": r["company_name"],
        "event_type": r["event_type"], "start_date": r["start_date"],
        "end_date": r["end_date"], "status": r["status"], "confidence": r["confidence"],
        "sources": sources, "origin": origin,
    }


def _in_window(r: dict, lo: str, hi: str) -> bool:
    sd = r.get("start_date")
    return (bool(sd) and not r.get("date_imprecise") and lo <= sd <= hi
            and r.get("event_type") in page.ANALYST_DAY_TYPES)


def merge(backfill_rows: list[dict], live_rows: list[dict],
          event_start: date, event_end: date) -> tuple[list[dict], dict]:
    lo, hi = event_start.isoformat(), event_end.isoformat()
    live_in = [r for r in live_rows if _in_window(r, lo, hi)]
    live = [r for r in live_in if r["status"] not in page.TERMINAL_STATUSES]
    back = [r for r in backfill_rows if _in_window(r, lo, hi) and r["status"] == "historical"]

    merged: dict[tuple, dict] = {}
    for r in live:
        merged[(r["ticker"], r["event_type"], r["start_date"])] = _item(r, "live")
    clashes = []
    # One marquee day per company per date. The same day reaches the two databases under
    # different types (Lilly's 2026-12-07 is an R&D Day in one source and an Investor Day in
    # another), and a key that includes the type would list it twice. The live row wins.
    live_days = {(k[0], k[2]) for k in merged}
    for r in back:
        key = (r["ticker"], r["event_type"], r["start_date"])
        if key in merged or (r["ticker"], r["start_date"]) in live_days:
            clashes.append(list(key))
            continue
        merged[key] = _item(r, "backfill")
    items = sorted(merged.values(),
                   key=lambda i: (i["start_date"], i["ticker"], i["event_type"]))
    stats = {
        "window": [lo, hi],
        "live_rows_total": len(live_rows),
        "live_in_window": len(live),
        "live_in_window_retired_excluded": len(live_in) - len(live),
        "backfill_historical_in_window": len(back),
        "backfill_rows_not_listed": sum(1 for r in backfill_rows if r["status"] != "historical"),
        "clashes_live_preferred": clashes,
        "items": len(items),
        "by_origin": {o: sum(1 for i in items if i["origin"] == o) for o in ("live", "backfill")},
    }
    return items, stats


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--event-start", required=True)
    ap.add_argument("--event-end", required=True)
    ap.add_argument("--backfill-db", type=Path, default=DEFAULT_BACKFILL_DB)
    ap.add_argument("--live-db", type=Path, default=page.DEFAULT_DB,
                    help="the fetched CI snapshot (default data/ci_snapshot/events.db)")
    ap.add_argument("--fetch", action="store_true",
                    help="download the newest successful master run's DB artifact first (gh)")
    ap.add_argument("--backfill-only", action="store_true",
                    help="write the backfill rows alone when the live snapshot cannot be had "
                         "(says so on stdout; every item then has origin 'backfill')")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    if args.fetch:
        prov = page.fetch_snapshot(args.live_db.parent)
        print(f"fetched snapshot: artifact {prov['artifact_created_at']} from {prov['run_url']}")
    if not args.backfill_db.exists():
        print(f"ERROR: backfill DB not found at {args.backfill_db}. Nothing written.",
              file=sys.stderr)
        return 2
    have_live = args.live_db.exists()
    if not have_live and not args.backfill_only:
        print(f"ERROR: live snapshot not found at {args.live_db}. Nothing written. Run with "
              "--fetch, or pass --backfill-only to write the backfill rows alone.",
              file=sys.stderr)
        return 2
    if not have_live:
        print(f"WARNING: no live snapshot at {args.live_db}: writing BACKFILL ROWS ONLY. "
              "This file is not the merged list until it is rebuilt with --fetch.")

    items, stats = merge(_rows(args.backfill_db), _rows(args.live_db) if have_live else [],
                         date.fromisoformat(args.event_start), date.fromisoformat(args.event_end))
    stats["live_side"] = "merged" if have_live else "MISSING - backfill rows only"
    prov_path = args.live_db.parent / page.PROVENANCE_NAME
    if have_live and prov_path.exists():
        prov = json.loads(prov_path.read_text(encoding="utf-8"))
        stats["live_snapshot"] = {k: prov.get(k) for k in
                                  ("artifact_created_at", "run_url", "fetched_at")}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(items)} event(s) -> {args.out}")
    print("stats: " + json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
