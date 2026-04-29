"""analyst-days CLI.

Modes (Phase 1):
  --discover                   scan_8k + scan_tavily + classify for each ticker,
                               upsert events into events.db
  --discover --dry-run         same, but no DB writes — prints proposed events
  --status                     print DB stats + upcoming events
  --weekly                     (TODO) discover → remind → digest one-shot
  --remind                     (TODO) reminder fan-out
  --digest                     (TODO) weekly digest

Phases 2+ swap the universe iterator (HC Services / MedTech / full universe);
the rest of the pipeline is universe-agnostic.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from src.discovery.classify import (
    ExtractionResult,
    classify_ticker,
    get_client,
)
from src.discovery.scan_8k import scan_ticker as edgar_scan
from src.discovery.scan_tavily import search_ticker as tavily_search
from src.outputs import gcal as gcal_out
from src.outputs import slack as slack_out
from src.outputs import ticktick as ticktick_out
from src.state.events_repo import (
    CandidateEvent,
    CandidateSource,
    PUSHABLE_EVENT_TYPES,
    recompute_statuses,
    upcoming_events,
    upsert_event,
    tentative_events,
)
from src.state.schema import init_db, schema_version, CURRENT_SCHEMA_VERSION
from src.universe import Ticker, load_core_watchlist


CONFIRMED_STATUSES = ("confirmed", "reminded_30", "reminded_7", "day_of")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULT_DB = "data/events.db"
DEFAULT_LOOKBACK_DAYS = 14


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Conversion: ExtractedEvent (LLM output) → CandidateEvent (DB write shape)
# --------------------------------------------------------------------------


def _to_candidate(
    ticker: Ticker,
    extracted,
    edgar_hit_urls: set[str],
) -> CandidateEvent:
    """Build a CandidateEvent (with sources) from one classifier output row."""
    src = CandidateSource(
        source_type=extracted.source_type,
        source_url=extracted.source_url,
        source_excerpt=extracted.rationale,
    )
    # If classifier picked an EDGAR URL but mis-tagged source_type, normalize.
    if extracted.source_url and extracted.source_url in edgar_hit_urls:
        src.source_type = "8K"

    return CandidateEvent(
        ticker=ticker.ticker,
        company_name=ticker.company_name,
        event_type=extracted.event_type,
        start_date=extracted.start_date,
        end_date=extracted.end_date,
        multi_day=bool(extracted.multi_day),
        date_imprecise=bool(extracted.date_imprecise),
        imprecise_hint=extracted.imprecise_hint,
        confidence=float(extracted.confidence),
        sources=[src],
    )


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    universe = load_core_watchlist()
    if args.tickers:
        wanted = {t.upper() for t in args.tickers}
        universe = [t for t in universe if t.ticker.upper() in wanted]
    if args.limit and args.limit > 0:
        universe = universe[: args.limit]

    print(f"[discover] universe={len(universe)} dry_run={args.dry_run} "
          f"lookback_days={args.lookback}")
    print()

    client = get_client()

    # DB connection only opens when we'll actually write.
    conn = None
    if not args.dry_run:
        conn = init_db(args.db)
        assert schema_version(conn) == CURRENT_SCHEMA_VERSION

    summary = {
        "tickers_scanned": 0,
        "edgar_hits_total": 0,
        "tavily_hits_total": 0,
        "events_extracted": 0,
        "events_inserted": 0,
        "events_merged": 0,
        "errors": 0,
    }

    for t in universe:
        print(f"=== {t.ticker} ({t.company_name}) ===")
        try:
            edgar_hits = edgar_scan(
                t.ticker, t.cik, lookback_days=args.lookback
            )
        except Exception as e:
            print(f"  EDGAR error: {type(e).__name__}: {e}")
            edgar_hits = []
            summary["errors"] += 1

        try:
            tavily_hits = tavily_search(
                t.ticker, t.company_name, max_results=args.tavily_results
            )
        except Exception as e:
            print(f"  Tavily error: {type(e).__name__}: {e}")
            tavily_hits = []
            summary["errors"] += 1

        summary["tickers_scanned"] += 1
        summary["edgar_hits_total"] += len(edgar_hits)
        summary["tavily_hits_total"] += len(tavily_hits)

        print(f"  EDGAR: {len(edgar_hits)} hit(s)  Tavily: {len(tavily_hits)} hit(s)")

        if not edgar_hits and not tavily_hits:
            print()
            continue

        try:
            result: ExtractionResult = classify_ticker(
                client,
                t.ticker,
                t.company_name,
                [h.to_dict() for h in edgar_hits],
                [h.to_dict() for h in tavily_hits],
            )
        except Exception as e:
            print(f"  Classifier error: {type(e).__name__}: {e}")
            summary["errors"] += 1
            print()
            continue

        edgar_urls = {h.url for h in edgar_hits}
        summary["events_extracted"] += len(result.events)

        for e in result.events:
            print(
                f"  -> {e.event_type:20} start={e.start_date or '?':10}  "
                f"multi={e.multi_day}  imprecise={e.date_imprecise}  "
                f"conf={e.confidence:.2f}"
            )
            print(f"     {e.rationale[:160]}")
            if args.dry_run:
                continue

            cand = _to_candidate(t, e, edgar_urls)
            event_id, status, is_new = upsert_event(conn, cand)
            if is_new:
                summary["events_inserted"] += 1
            else:
                summary["events_merged"] += 1
            print(f"     -> DB event_id={event_id} status={status} "
                  f"{'(new)' if is_new else '(merged)'}")
        print()

    # End-of-run fan-out: idempotent, retries failures next run.
    if conn is not None and not args.dry_run:
        fan_summary = _fan_out_confirmed(conn, args)
        summary.update(fan_summary)
        conn.close()

    print("---")
    print(f"summary: {json.dumps(summary, indent=2)}")
    return 0


def _fan_out_confirmed(conn, args: argparse.Namespace) -> dict:
    """Post any confirmed event missing slack/calendar/ticktick output rows.

    Idempotent — uses the events.{slack_posted_at, calendar_event_id,
    ticktick_task_id} columns to skip already-fanned-out events. A
    failed channel will simply be retried on the next run.

    Runs recompute_statuses() first so events that newly clear their
    per-type threshold (e.g. after a threshold change) get fanned out
    on this run rather than waiting for the next discover.
    """
    promoted = recompute_statuses(conn)
    if promoted:
        print(f"[fan-out] promoted {promoted} discovered/tentative -> confirmed")

    status_placeholders = ",".join(["?"] * len(CONFIRMED_STATUSES))
    type_placeholders = ",".join(["?"] * len(PUSHABLE_EVENT_TYPES))
    pushable_types = sorted(PUSHABLE_EVENT_TYPES)
    rows = conn.execute(
        f"SELECT * FROM events WHERE status IN ({status_placeholders}) "
        f"AND event_type IN ({type_placeholders}) "
        "AND start_date IS NOT NULL ORDER BY start_date ASC",
        (*CONFIRMED_STATUSES, *pushable_types),
    ).fetchall()

    if not rows:
        return {"fanout_slack": 0, "fanout_gcal": 0, "fanout_ticktick": 0}

    slack_posted = 0
    gcal_posted = 0
    ticktick_posted = 0

    gcal_service = None
    ticktick_list_id: Optional[str] = None
    ticktick_disabled = False  # set true on auth/list failure to stop retrying
    print(f"[fan-out] {len(rows)} confirmed event(s) to evaluate")

    for row in rows:
        # Slack
        if not row["slack_posted_at"] and not args.no_slack:
            try:
                slack_out.post_confirmed(row)
                conn.execute(
                    "UPDATE events SET slack_posted_at = ? WHERE id = ?",
                    (_utcnow(), row["id"]),
                )
                conn.commit()
                slack_posted += 1
                print(f"  Slack: {row['ticker']} {row['event_type']} -> posted")
            except Exception as exc:
                print(f"  Slack: {row['ticker']} -> FAILED ({type(exc).__name__}: {exc})")

        # Calendar
        if not row["calendar_event_id"] and not args.no_gcal:
            if gcal_service is None:
                try:
                    gcal_service = gcal_out.get_service()
                except Exception as exc:
                    print(f"  Calendar: auth FAILED ({type(exc).__name__}: {exc}); "
                          "skipping calendar fan-out")
                    gcal_service = False  # sentinel: don't retry auth this run
            if gcal_service:
                try:
                    gcal_out.upsert_calendar_event(gcal_service, conn, row)
                    gcal_posted += 1
                    print(f"  Calendar: {row['ticker']} {row['event_type']} -> posted")
                except Exception as exc:
                    print(f"  Calendar: {row['ticker']} -> FAILED ({type(exc).__name__}: {exc})")

        # TickTick
        if not row["ticktick_task_id"] and not args.no_ticktick and not ticktick_disabled:
            if ticktick_list_id is None:
                try:
                    ticktick_list_id = ticktick_out.find_or_create_list()
                except Exception as exc:
                    print(f"  TickTick: auth/list FAILED "
                          f"({type(exc).__name__}: {exc}); skipping fan-out")
                    ticktick_disabled = True
            if ticktick_list_id and not ticktick_disabled:
                try:
                    ticktick_out.upsert_event_task(conn, row, ticktick_list_id)
                    ticktick_posted += 1
                    print(f"  TickTick: {row['ticker']} {row['event_type']} -> posted")
                except ticktick_out.TickTickTokenExpired:
                    print("  TickTick: token expired — skipping rest")
                    ticktick_disabled = True
                except Exception as exc:
                    print(f"  TickTick: {row['ticker']} -> FAILED ({type(exc).__name__}: {exc})")

    return {
        "fanout_slack": slack_posted,
        "fanout_gcal": gcal_posted,
        "fanout_ticktick": ticktick_posted,
    }


def cmd_status(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"No DB at {db_path}. Run --discover to populate.")
        return 0
    conn = init_db(args.db)
    today = date.today().isoformat()

    counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM events GROUP BY status"
    ).fetchall())
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(f"DB: {args.db}  schema_version={schema_version(conn)}")
    print(f"  events_total: {total}")
    for s, n in sorted(counts.items()):
        print(f"  {s:20} {n}")
    print()

    upcoming = upcoming_events(conn, today, horizon_days=60)
    print(f"Upcoming (next 60d): {len(upcoming)}")
    for r in upcoming[:20]:
        print(
            f"  {r['start_date']}  {r['ticker']:6}  {r['event_type']:20}  "
            f"conf={r['confidence']:.2f}  status={r['status']}"
        )

    tentative = tentative_events(conn)
    if tentative:
        print()
        print(f"Tentative ({len(tentative)}):")
        for r in tentative[:20]:
            print(
                f"  {r['ticker']:6}  {r['event_type']:20}  "
                f"hint={r['imprecise_hint']!r}  conf={r['confidence']:.2f}"
            )

    conn.close()
    return 0


# --------------------------------------------------------------------------
# Argparse
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="analyst-days")
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite DB path")

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--discover", action="store_true",
                      help="Scan EDGAR + Tavily, classify, upsert events")
    mode.add_argument("--status", action="store_true",
                      help="Print DB stats + upcoming events")
    mode.add_argument("--friday-digest", action="store_true",
                      help="Post Friday 'on the radar' digest to #analyst-days")
    mode.add_argument("--monday-digest", action="store_true",
                      help="Post Monday 'forward 30/7' digest to #analyst-days")
    mode.add_argument("--slack-test", action="store_true",
                      help="Post a sanity ping to #analyst-days")
    mode.add_argument("--gcal-test", action="store_true",
                      help="Verify Google Calendar auth + access (no writes)")
    mode.add_argument("--ticktick-test", action="store_true",
                      help="Verify TickTick auth and find/create the Analyst Days list")
    mode.add_argument("--fanout", action="store_true",
                      help="Re-run output fan-out without scanning (retries Slack/Calendar)")
    mode.add_argument("--prune-non-pushable", action="store_true",
                      help="Delete Calendar entries + clear slack_posted_at "
                           "for non-pushable types (e.g. conferences) "
                           "after a policy change. Idempotent.")
    mode.add_argument("--remind", action="store_true",
                      help="(TODO) Reminder fan-out (T-30 / T-7 / day-of)")

    p.add_argument("--dry-run", action="store_true",
                   help="No DB writes / no fan-out; prints proposed actions")
    p.add_argument("--no-slack", action="store_true",
                   help="Skip Slack posts even outside --dry-run")
    p.add_argument("--no-gcal", action="store_true",
                   help="Skip Google Calendar posts even outside --dry-run")
    p.add_argument("--no-ticktick", action="store_true",
                   help="Skip TickTick posts even outside --dry-run")
    p.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_DAYS,
                   help="EDGAR lookback window in days (default 14)")
    p.add_argument("--tavily-results", type=int, default=5,
                   help="Tavily max_results per ticker")
    p.add_argument("--tickers", nargs="*",
                   help="Restrict to these tickers (subset of core watchlist)")
    p.add_argument("--limit", type=int, default=0,
                   help="Process at most N tickers (0 = all)")
    return p


def cmd_friday_digest(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"No DB at {db_path}. Run --discover first.")
        return 1
    conn = init_db(args.db)
    try:
        n = slack_out.post_friday_digest(conn)
        print(f"Friday digest posted to #analyst-days  ({n} events)")
        return 0
    finally:
        conn.close()


def cmd_monday_digest(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"No DB at {db_path}. Run --discover first.")
        return 1
    conn = init_db(args.db)
    try:
        n = slack_out.post_monday_digest(conn)
        print(f"Monday digest posted to #analyst-days  ({n} events in 30d)")
        return 0
    finally:
        conn.close()


def cmd_slack_test(args: argparse.Namespace) -> int:
    slack_out.post_test()
    print("Slack sanity ping posted to #analyst-days")
    return 0


def cmd_gcal_test(args: argparse.Namespace) -> int:
    gcal_out.smoke_test()
    return 0


def cmd_ticktick_test(args: argparse.Namespace) -> int:
    ticktick_out.smoke_test()
    return 0


def cmd_fanout(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"No DB at {db_path}.")
        return 1
    conn = init_db(args.db)
    try:
        result = _fan_out_confirmed(conn, args)
        print(f"summary: {json.dumps(result, indent=2)}")
        return 0
    finally:
        conn.close()


def cmd_prune_non_pushable(args: argparse.Namespace) -> int:
    """Clean up Calendar entries (and reset slack_posted_at) for events
    whose event_type is no longer in PUSHABLE_EVENT_TYPES.

    Use after a policy change — e.g. when conferences moved from pushable
    to tracked-only.
    """
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"No DB at {db_path}.")
        return 1
    conn = init_db(args.db)
    try:
        type_placeholders = ",".join(["?"] * len(PUSHABLE_EVENT_TYPES))
        pushable_types = sorted(PUSHABLE_EVENT_TYPES)
        # Find non-pushable events that have artifacts attached.
        rows = conn.execute(
            f"SELECT id, ticker, event_type, calendar_event_id, slack_posted_at "
            f"FROM events WHERE event_type NOT IN ({type_placeholders}) "
            "AND (calendar_event_id IS NOT NULL OR slack_posted_at IS NOT NULL) "
            "ORDER BY id",
            pushable_types,
        ).fetchall()

        if not rows:
            print("Nothing to prune.")
            return 0

        gcal_service = None
        gcal_deleted = 0
        slack_cleared = 0
        for r in rows:
            print(f"  {r['ticker']:6}  {r['event_type']:14}  "
                  f"gcal={'Y' if r['calendar_event_id'] else 'N'}  "
                  f"slack_posted={'Y' if r['slack_posted_at'] else 'N'}")
            if r["calendar_event_id"]:
                if gcal_service is None:
                    try:
                        gcal_service = gcal_out.get_service()
                    except Exception as exc:
                        print(f"    Calendar auth failed: {exc}")
                        gcal_service = False
                if gcal_service:
                    if gcal_out.delete_calendar_event(gcal_service, conn, r["id"]):
                        gcal_deleted += 1
                        print("    -> deleted from Calendar")
        # Wipe slack_posted_at on all non-pushable rows in one shot
        slack_cleared = conn.execute(
            f"UPDATE events SET slack_posted_at = NULL "
            f"WHERE event_type NOT IN ({type_placeholders}) "
            "AND slack_posted_at IS NOT NULL",
            pushable_types,
        ).rowcount
        conn.commit()
        print(f"\nPruned {gcal_deleted} calendar event(s); "
              f"cleared slack_posted_at on {slack_cleared} row(s).")
        print("Note: Slack messages already in #analyst-days history can't be "
              "deleted via webhook. Delete manually or ignore.")
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.discover:
        return cmd_discover(args)
    if args.status:
        return cmd_status(args)
    if args.friday_digest:
        return cmd_friday_digest(args)
    if args.monday_digest:
        return cmd_monday_digest(args)
    if args.slack_test:
        return cmd_slack_test(args)
    if args.gcal_test:
        return cmd_gcal_test(args)
    if args.ticktick_test:
        return cmd_ticktick_test(args)
    if args.fanout:
        return cmd_fanout(args)
    if args.prune_non_pushable:
        return cmd_prune_non_pushable(args)
    if args.remind:
        print("Mode not yet implemented (Phase 1 ships --discover, --status, --friday-digest, --monday-digest, --slack-test).")
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
