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
from src.state.events_repo import (
    CandidateEvent,
    CandidateSource,
    upcoming_events,
    upsert_event,
    tentative_events,
)
from src.state.schema import init_db, schema_version, CURRENT_SCHEMA_VERSION
from src.universe import Ticker, load_core_watchlist


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

    if conn is not None:
        conn.close()

    print("---")
    print(f"summary: {json.dumps(summary, indent=2)}")
    return 0


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
    mode.add_argument("--remind", action="store_true",
                      help="(TODO) Reminder fan-out (T-30 / T-7 / day-of)")
    mode.add_argument("--digest", action="store_true",
                      help="(TODO) Weekly Monday digest")
    mode.add_argument("--weekly", action="store_true",
                      help="(TODO) discover → remind → digest in sequence")

    p.add_argument("--dry-run", action="store_true",
                   help="No DB writes / no fan-out; prints proposed actions")
    p.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_DAYS,
                   help="EDGAR lookback window in days (default 14)")
    p.add_argument("--tavily-results", type=int, default=5,
                   help="Tavily max_results per ticker")
    p.add_argument("--tickers", nargs="*",
                   help="Restrict to these tickers (subset of core watchlist)")
    p.add_argument("--limit", type=int, default=0,
                   help="Process at most N tickers (0 = all)")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.discover:
        return cmd_discover(args)
    if args.status:
        return cmd_status(args)
    if args.remind or args.digest or args.weekly:
        print("Mode not yet implemented (Phase 1 ships --discover and --status).")
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
