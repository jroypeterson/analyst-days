"""Publish a portable JSON of upcoming analyst/investor/R&D/capital-markets days
+ tracked conferences to exports/upcoming_events.json.

Consumer: sa-monitor (Phase 2 'Note:' context enrichment). Schema matches
sa-monitor/src/calendars.py:AnalystDayCalendar — see that file for the canonical
contract.

Window: events with start_date in [today - 1 day, today + 60 days]. Analyst
days are scheduled further in advance than earnings, so a longer lookahead
window is appropriate; the 1-day lookback covers same-day halts after a
mid-day analyst event start.

Run from the analyst-days repo root:
    python scripts/export_upcoming_events.py

Output is intended to be committed to the repo so sa-monitor's CI can fetch it
via raw.githubusercontent.com.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "events.db"
DEFAULT_OUT = REPO_ROOT / "exports" / "upcoming_events.json"

SCHEMA_VERSION = 1
WINDOW_LOOKBACK_DAYS = 1
WINDOW_LOOKAHEAD_DAYS = 60


def export(db_path: Path, out_path: Path,
           *, lookback_days: int = WINDOW_LOOKBACK_DAYS,
           lookahead_days: int = WINDOW_LOOKAHEAD_DAYS) -> int:
    """Write the upcoming-events JSON. Returns the count of events written."""
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return -1

    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=lookback_days)).isoformat()
    end = (today + timedelta(days=lookahead_days)).isoformat()

    con = sqlite3.connect(db_path)
    rows = con.execute(
        """
        SELECT ticker, company_name, event_type, start_date, end_date,
               COALESCE(multi_day, 0), COALESCE(status, '')
        FROM events
        WHERE start_date BETWEEN ? AND ?
          AND COALESCE(status, '') != 'cancelled'
        ORDER BY start_date, ticker
        """,
        (start, end),
    ).fetchall()
    con.close()

    events = []
    for ticker, name, event_type, sd, ed, multi_day, status in rows:
        events.append({
            "ticker": (ticker or "").upper(),
            "company_name": name or "",
            "event_type": event_type or "",
            "start_date": sd,
            "end_date": ed,
            "multi_day": bool(multi_day),
            "status": status,
        })

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": "analyst-days",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "window": {"start": start, "end": end},
        "counts": {"events": len(events)},
        "events": events,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return len(events)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export upcoming analyst-day events to JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"Path to events.db (default: {DEFAULT_DB})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"Output JSON path (default: {DEFAULT_OUT})")
    parser.add_argument("--lookback-days", type=int, default=WINDOW_LOOKBACK_DAYS)
    parser.add_argument("--lookahead-days", type=int, default=WINDOW_LOOKAHEAD_DAYS)
    args = parser.parse_args(argv)

    n = export(args.db, args.out,
               lookback_days=args.lookback_days,
               lookahead_days=args.lookahead_days)
    if n < 0:
        return 1
    print(f"wrote {n} events to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
