"""Reminder state machine — T-30 / T-7 / day-of pings for confirmed events.

Cadence is weekly (the Monday fire), not daily. So the three reminder
windows are sized to consecutive ~1-week buckets that, run once a week,
each catch a confirmed event exactly once on its way to the event date:

    t30     14 <= days_until <= 30   "~30 days out"   (first heads-up)
    t7       7 <= days_until <= 13   "1 week out"
    day_of   0 <= days_until <=  6   "happening this week"

The windows are disjoint, so an event matches at most one bucket per run.
Each transition is **one-shot**, guarded by the events.{reminded_30_at,
reminded_7_at, day_of_at} columns — once a bucket has fired for an event it
never re-fires, even if a later weekly run still sees the event inside the
same window. An event first discovered late (e.g. already 5 days out) simply
starts at the day_of bucket; it doesn't retro-fire the earlier ones.

Only confirmed (or already-reminded) pushable events with a precise
start_date are eligible — tentative / imprecise events never get reminders
(they live in the Friday radar until a precise corroborating source arrives).

Reminders fail loud: a Slack post failure is surfaced in the summary and the
status/_at columns are NOT advanced, so the next run retries.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from typing import Optional

from src.outputs import slack as slack_out
from src.state.events_repo import PUSHABLE_EVENT_TYPES

# Eligible source statuses — confirmed plus any already-reminded state, so a
# previously-reminded event can still advance to the next bucket.
ELIGIBLE_STATUSES = ("confirmed", "reminded_30", "reminded_7", "day_of")

# (kind, status, ts_column, lo_days, hi_days) — inclusive day windows.
REMINDER_KINDS = (
    ("t30", "reminded_30", "reminded_30_at", 14, 30),
    ("t7", "reminded_7", "reminded_7_at", 7, 13),
    ("day_of", "day_of", "day_of_at", 0, 6),
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _days_until(start_date_iso: str, today: date) -> int:
    return (date.fromisoformat(start_date_iso) - today).days


def due_reminders(conn: sqlite3.Connection, today_iso: str) -> list[tuple]:
    """Return [(event_row, kind, status, ts_column), ...] for events that
    should be reminded on this run. Pure read — no DB writes, no Slack.
    """
    today = date.fromisoformat(today_iso)
    type_placeholders = ",".join(["?"] * len(PUSHABLE_EVENT_TYPES))
    status_placeholders = ",".join(["?"] * len(ELIGIBLE_STATUSES))
    # Join the primary source so the reminder ping can carry the announcement
    # link, same as the confirmation ping.
    rows = conn.execute(
        f"SELECT e.*, "
        f" (SELECT s.source_type FROM event_sources s "
        f"   WHERE s.event_id = e.id ORDER BY s.id ASC LIMIT 1) AS source_type, "
        f" (SELECT s.source_url FROM event_sources s "
        f"   WHERE s.event_id = e.id ORDER BY s.id ASC LIMIT 1) AS source_url "
        f"FROM events e "
        f"WHERE e.status IN ({status_placeholders}) "
        f"AND e.event_type IN ({type_placeholders}) "
        f"AND e.start_date IS NOT NULL AND e.date_imprecise = 0 "
        f"ORDER BY e.start_date ASC",
        (*ELIGIBLE_STATUSES, *sorted(PUSHABLE_EVENT_TYPES)),
    ).fetchall()

    out: list[tuple] = []
    for r in rows:
        du = _days_until(r["start_date"], today)
        for kind, status, ts_col, lo, hi in REMINDER_KINDS:
            if lo <= du <= hi and not r[ts_col]:
                out.append((r, kind, status, ts_col))
                break  # windows are disjoint — at most one per event per run
    return out


def run_reminders(
    conn: sqlite3.Connection,
    today_iso: Optional[str] = None,
    *,
    dry_run: bool = False,
    no_slack: bool = False,
) -> dict:
    """Fire any due reminders. Returns a summary dict.

    Idempotent + one-shot: each bucket stamps its _at column on success so a
    re-run won't re-ping. On Slack failure the column is left unset (retry
    next run) and the failure is counted, never swallowed silently.
    """
    today_iso = today_iso or date.today().isoformat()
    due = due_reminders(conn, today_iso)

    summary = {
        "today": today_iso,
        "due": len(due),
        "t30": 0,
        "t7": 0,
        "day_of": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    for row, kind, status, ts_col in due:
        label = f"{row['ticker']} {row['event_type']} ({kind}, {row['start_date']})"
        if dry_run:
            print(f"  [dry-run] would remind: {label}")
            summary[kind] += 1
            continue
        try:
            if not no_slack:
                slack_out.post_reminder(row, kind)
            # Advance status + stamp the one-shot guard column atomically.
            conn.execute(
                f"UPDATE events SET status = ?, {ts_col} = ? WHERE id = ?",
                (status, _utcnow(), row["id"]),
            )
            conn.commit()
            summary[kind] += 1
            print(f"  reminded: {label}")
        except Exception as exc:  # noqa: BLE001 — surface, don't swallow
            summary["errors"] += 1
            print(f"  REMINDER FAILED: {label} -> {type(exc).__name__}: {exc}")

    return summary
