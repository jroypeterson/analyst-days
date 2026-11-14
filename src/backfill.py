"""Bounded historical backfill of analyst / investor / R&D / capital markets days.

    python -m src.backfill --db data/backfill_2yr.db \
        --event-start 2024-11-14 --event-end 2026-11-14 --filings-from 2024-09-01

A bounded cut of "Phase 4" (2026-11-14): the events HELD inside a window, found by the
same EDGAR 8-K / 6-K full-text search the weekly run uses, swept one calendar month at
a time, classified in past-events mode and stored with `status='historical'`.

What this module is careful about, and why:

* **It can reach no output channel.** It imports neither `src.cli` nor `src.outputs`,
  `src.reminders`, `src.digest` or `src.health`, so there is no Slack, Calendar,
  TickTick or email code in the process to call. `tests/test_backfill.py` asserts that
  on the loaded module set of a fresh interpreter, not on a list of names here.
* **It refuses the production database.** `data/events.db`, anything under
  `data/ci_snapshot/`, and any run under GitHub Actions are rejected before a file is
  opened: the artifact flow restores and re-uploads `data/events.db`, and several
  thousand historical rows uploaded as state is not something a later run can undo.
* **EDGAR only.** No web search. An event announced only on an IR calendar or in an
  unfiled press release (Revvity's 2026 Investor Day, announced 2026-10-06, was not
  filed until the Q3 8-K) is invisible to this pass.
* **The gates are production's.** A row becomes `historical` only with a precise date
  grounded in the raw excerpt of the filing the classifier cited, confidence at the
  per-type bar, and an SEC filing as its source (`upsert_event(historical=True)`).
  An in-window event that misses a gate is stored `discovered` so it can be reviewed,
  and is not part of the published list.
* **A month that failed is not a month with no filings.** A window whose search raises
  is recorded in `windows_failed`, is not cached, and makes the run exit 1.

Both expensive steps are resumable: every completed month is written to the hits cache,
and every classifier reply to the classified log, keyed on the prompt and the filings
in the batch. A re-run repeats neither.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from src.discovery import classify as classify_mod
from src.discovery import scan_edgar_fts
from src.discovery.date_grounding import event_date_grounded, grounded_in_cited_source
from src.discovery.scan_edgar import EdgarHit, EdgarScanError
from src.state.events_repo import (
    PUSHABLE_EVENT_TYPES,
    CandidateEvent,
    CandidateSource,
    threshold_for,
    upsert_event,
)
from src.state.schema import init_db
from src.universe import load_scan_universe

DEFAULT_DB = "data/backfill_2yr.db"
DEFAULT_HITS_CACHE = "data/backfill_edgar_hits.json"
DEFAULT_CLASSIFIED_LOG = "data/backfill_classified.jsonl"
DEFAULT_STATS = "data/backfill_run_stats.json"

# Filings handed to the classifier in one call. A name can have 40+ trigger filings
# in two years; one bundle of all of them buries the dated announcement.
BATCH_SIZE = 8
CLASSIFY_WORKERS = 4

# Paths this module will not write a database to. Relative to the working directory,
# like every data path in this project.
_PRODUCTION_DB = Path("data") / "events.db"
_SNAPSHOT_DIR = Path("data") / "ci_snapshot"


class BackfillRefused(RuntimeError):
    """The run would have touched production state. Raised before anything is opened."""


def assert_safe_db(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if os.environ.get("GITHUB_ACTIONS"):
        raise BackfillRefused(
            "the backfill does not run under GitHub Actions: the workflows restore and "
            "re-upload data/events.db, and this pass must never be part of that flow")
    if p == _PRODUCTION_DB.resolve():
        raise BackfillRefused(
            f"{db_path} is the production events database; the backfill writes to its "
            f"own file (default {DEFAULT_DB})")
    snap = _SNAPSHOT_DIR.resolve()
    if p == snap or snap in p.parents:
        raise BackfillRefused(f"{db_path} is inside the fetched CI snapshot, which is read-only")
    return p


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar-month chunks covering [start, end] inclusive: no gap, no overlap.

    EDGAR full-text search will not page past 10,000 results and two years of the
    trigger phrases is about that many, so the sweep cannot be one query. A month is
    150-550 documents (measured 2026-11-14)."""
    if end < start:
        raise ValueError(f"window end {end} is before its start {start}")
    out: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        out.append((cur, min(end, nxt - timedelta(days=1))))
        cur = nxt
    return out


def _window_key(w: tuple[date, date]) -> str:
    return f"{w[0].isoformat()}..{w[1].isoformat()}"


def _load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    tmp.replace(path)


def sweep(cik_to_tickers: dict[str, list[str]], windows: list[tuple[date, date]],
          cache_path: Path, *, get: Optional[Callable] = None,
          log: Callable[[str], None] = print) -> dict:
    """Run the full-text scan for every window not already cached.

    Returns the cache: {window key: {documents_searched, filings_matched, errors,
    hits}} for completed windows, plus the failures of this call under `_failed`.
    """
    cache: dict = _load_json(cache_path, {})
    cache.pop("_failed", None)
    failed: dict[str, str] = {}
    kwargs = {"get": get} if get is not None else {}
    for w in windows:
        key = _window_key(w)
        if key in cache:
            continue
        try:
            res = scan_edgar_fts.scan_universe(
                cik_to_tickers, lookback_days=(w[1] - w[0]).days, today=w[1], **kwargs)
        except EdgarScanError as e:
            failed[key] = str(e)
            log(f"[sweep] {key}: FAILED - {e}")
            continue
        cache[key] = {
            "documents_searched": res.documents_searched,
            "filings_matched": res.filings_matched,
            "errors": res.errors,
            "hits": [h.to_dict() for hits in res.hits_by_ticker.values() for h in hits],
        }
        _write_json(cache_path, cache)
        log(f"[sweep] {key}: {res.documents_searched} document(s), "
            f"{res.filings_matched} filing(s) by scan-set filers, {res.hit_count} hit(s), "
            f"{len(res.errors)} fetch error(s)")
    cache["_failed"] = failed
    return cache


def hits_by_ticker(cache: dict, windows: list[tuple[date, date]]) -> dict[str, list[dict]]:
    """Cached hits for these windows, one per (ticker, filing), oldest filing first."""
    seen: set[tuple[str, str]] = set()
    out: dict[str, list[dict]] = {}
    for w in windows:
        for h in (cache.get(_window_key(w)) or {}).get("hits", []):
            k = (h["ticker"], h["accession"])
            if k in seen:
                continue
            seen.add(k)
            out.setdefault(h["ticker"], []).append(h)
    for hits in out.values():
        hits.sort(key=lambda h: (h["filing_date"], h["accession"]))
    return out


def _batches(hits: list[dict], size: int = BATCH_SIZE) -> list[list[dict]]:
    return [hits[i:i + size] for i in range(0, len(hits), size)]


def _batch_key(ticker: str, batch: list[dict]) -> str:
    """Identifies one classifier call: the prompt, the model, and the filings read."""
    h = hashlib.sha256()
    h.update(classify_mod.PAST_EVENTS_SYSTEM_PROMPT.encode("utf-8"))
    h.update(classify_mod.MODEL.encode("utf-8"))
    for hit in batch:
        h.update(f"|{hit['accession']}|{hit['url']}".encode("utf-8"))
    return f"{ticker}:{h.hexdigest()[:16]}"


def _load_classified(path: Path) -> dict[str, list[dict]]:
    done: dict[str, list[dict]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["key"]] = rec["events"]
    return done


def classify_all(by_ticker: dict[str, list[dict]], names: dict[str, str], log_path: Path,
                 *, classify: Callable, client, today_iso: str, workers: int = CLASSIFY_WORKERS,
                 log: Callable[[str], None] = print) -> tuple[list[tuple[str, list[dict], list[dict]]], dict]:
    """Classify every batch not already in the log.

    Returns ([(ticker, batch, events)], counters). A batch whose call raised is
    counted, named, and NOT logged, so a re-run asks again.
    """
    done = _load_classified(log_path)
    work: list[tuple[str, list[dict], str]] = [
        (t, b, _batch_key(t, b)) for t in sorted(by_ticker) for b in _batches(by_ticker[t])]
    counters = {"batches": len(work), "batches_from_log": 0, "batches_called": 0,
                "classifier_errors": 0, "classifier_error_detail": []}

    def call(item):
        ticker, batch, key = item
        try:
            res = classify(client, ticker, names.get(ticker, ticker), batch, [],
                           today_iso=today_iso, past_events=True)
            return [e.model_dump() for e in res.events], None
        except Exception as e:                                   # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"

    todo = [w for w in work if w[2] not in done]
    counters["batches_from_log"] = len(work) - len(todo)
    if todo:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool, \
                log_path.open("a", encoding="utf-8") as fh:
            for n, (item, (events, err)) in enumerate(zip(todo, pool.map(call, todo)), 1):
                ticker, batch, key = item
                if err is not None:
                    counters["classifier_errors"] += 1
                    counters["classifier_error_detail"].append(f"{ticker}: {err}"[:300])
                    log(f"[classify] {ticker}: ERROR {err}")
                    continue
                counters["batches_called"] += 1
                done[key] = events
                fh.write(json.dumps({"key": key, "ticker": ticker,
                                     "accessions": [h["accession"] for h in batch],
                                     "events": events}) + "\n")
                fh.flush()
                if n % 25 == 0 or n == len(todo):
                    log(f"[classify] {n}/{len(todo)} batch(es)")
    out = [(t, b, done[k]) for t, b, k in work if k in done]
    return out, counters


def store(conn, results: list[tuple[str, list[dict], list[dict]]], names: dict[str, str],
          event_start: date, event_end: date, *, today: date,
          log: Callable[[str], None] = print) -> dict:
    """Apply the window and the gates, and write in-window rows to the backfill DB."""
    c = {"events_extracted": 0, "skipped_conference": 0, "skipped_imprecise": 0,
         "skipped_before_window": 0, "skipped_after_window": 0, "skipped_bad_date": 0,
         "in_window": 0, "in_window_ungrounded": 0, "in_window_below_confidence": 0,
         "cited_url_not_in_bundle": 0, "rows_inserted": 0, "rows_merged": 0}
    # "Held" needs the day to have arrived. upsert's test is `start_date < today_iso`,
    # so the reference is the day after the last day that counts.
    past_ref = (min(event_end, today) + timedelta(days=1)).isoformat()
    lo, hi = event_start.isoformat(), min(event_end, today).isoformat()

    for ticker, batch, events in results:
        text_by_url = {h["url"]: h.get("excerpt", "") for h in batch if h.get("url")}
        acc_by_url = {h["url"]: h["accession"] for h in batch if h.get("url")}
        for e in events:
            c["events_extracted"] += 1
            if e["event_type"] not in PUSHABLE_EVENT_TYPES:
                c["skipped_conference"] += 1
                continue
            sd = e.get("start_date")
            if e.get("date_imprecise") or not sd:
                c["skipped_imprecise"] += 1
                continue
            try:
                date.fromisoformat(sd)
            except ValueError:
                c["skipped_bad_date"] += 1
                continue
            if sd < lo:
                c["skipped_before_window"] += 1
                continue
            if sd > hi:
                c["skipped_after_window"] += 1
                continue
            c["in_window"] += 1

            url = e.get("source_url")
            grounded = grounded_in_cited_source(sd, False, url, text_by_url)
            if url not in text_by_url:
                # The model cited a URL that was not in its bundle. The gate fell back
                # to the bundle's texts; record as the source the filing that actually
                # carries the date, never the URL the model wrote.
                c["cited_url_not_in_bundle"] += 1
                url = next((u for u, t in text_by_url.items()
                            if event_date_grounded(sd, t)), None)
                grounded = grounded and url is not None
            if not grounded:
                c["in_window_ungrounded"] += 1
            elif float(e["confidence"]) < threshold_for(e["event_type"]):
                c["in_window_below_confidence"] += 1

            cand = CandidateEvent(
                ticker=ticker, company_name=names.get(ticker), event_type=e["event_type"],
                start_date=sd, end_date=e.get("end_date"), multi_day=bool(e.get("multi_day")),
                date_imprecise=False, imprecise_hint=None, confidence=float(e["confidence"]),
                date_grounded=bool(grounded),
                sources=[CandidateSource(
                    source_type="8K" if url else "TAVILY_HIT", source_url=url,
                    source_excerpt=e.get("rationale"), accession_no=acc_by_url.get(url))])
            _, status, is_new = upsert_event(conn, cand, today_iso=past_ref, historical=True)
            c["rows_inserted" if is_new else "rows_merged"] += 1
            log(f"  {ticker:6} {e['event_type']:20} {sd} conf={float(e['confidence']):.2f} "
                f"grounded={grounded} -> {status}{'' if is_new else ' (merged)'}")
    return c


def run(args: argparse.Namespace, *, get: Optional[Callable] = None,
        classify: Optional[Callable] = None, client=None,
        universe: Optional[list] = None, today: Optional[date] = None,
        log: Callable[[str], None] = print) -> int:
    db_path = assert_safe_db(args.db)
    today = today or date.today()
    event_start = date.fromisoformat(args.event_start)
    event_end = date.fromisoformat(args.event_end)
    filings_from = date.fromisoformat(args.filings_from)
    filings_to = min(date.fromisoformat(args.filings_to) if args.filings_to else today, today)
    if event_end < event_start or filings_to < filings_from:
        raise ValueError("a window ends before it starts")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    universe = universe if universe is not None else load_scan_universe()
    names = {t.ticker: t.company_name for t in universe}
    cik_to_tickers, no_cik = scan_edgar_fts.cik_map(universe)
    windows = month_windows(filings_from, filings_to)
    log(f"[backfill] events {event_start}..{event_end}, filings {filings_from}..{filings_to} "
        f"({len(windows)} window(s)); scan set {len(universe)} name(s), "
        f"{len(cik_to_tickers)} CIK(s), {len(no_cik)} with no CIK (not searchable on EDGAR)")

    cache = sweep(cik_to_tickers, windows, Path(args.hits_cache), get=get, log=log)
    failed = cache.get("_failed", {})
    by_ticker = hits_by_ticker(cache, windows)
    if args.tickers:
        wanted = {t.upper() for t in args.tickers}
        by_ticker = {t: h for t, h in by_ticker.items() if t.upper() in wanted}
    done_windows = [cache[_window_key(w)] for w in windows if _window_key(w) in cache]
    stats = {
        "started_at": started,
        "event_window": [event_start.isoformat(), event_end.isoformat()],
        "filing_window": [filings_from.isoformat(), filings_to.isoformat()],
        "scan_set": len(universe), "scan_set_ciks": len(cik_to_tickers),
        "scan_set_no_cik": sorted(no_cik),
        "ticker_filter": sorted(args.tickers or []),
        "windows": len(windows), "windows_completed": len(done_windows),
        "windows_failed": failed,
        "documents_searched": sum(w["documents_searched"] for w in done_windows),
        "filings_matched": sum(w["filings_matched"] for w in done_windows),
        "document_fetch_errors": [e for w in done_windows for e in w["errors"]],
        "edgar_hits": sum(len(h) for h in by_ticker.values()),
        "tickers_with_hits": len(by_ticker),
    }
    log(f"[backfill] {stats['edgar_hits']} EDGAR hit(s) across {len(by_ticker)} name(s); "
        f"{len(failed)} window(s) failed")

    rc = 1 if failed else 0
    if not args.sweep_only:
        results, counters = classify_all(
            by_ticker, names, Path(args.classified_log),
            classify=classify or classify_mod.classify_ticker,
            client=client if client is not None else classify_mod.get_client(),
            today_iso=today.isoformat(), workers=args.workers, log=log)
        stats.update(counters)
        stats["edgar_hits_classified"] = sum(len(b) for _, b, _ in results)
        if counters["classifier_errors"]:
            rc = 1
        if args.dry_run:
            log("[backfill] dry run: nothing written to the database")
        else:
            conn = init_db(db_path)
            stats.update(store(conn, results, names, event_start, event_end,
                               today=today, log=log))
            stats["db_status_counts"] = {
                r[0]: r[1] for r in conn.execute(
                    "SELECT status, COUNT(*) FROM events GROUP BY status")}
            conn.execute(
                "INSERT INTO run_log (run_type, started_at, finished_at, status, summary) "
                "VALUES ('backfill', ?, ?, ?, ?)",
                (started, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "ok" if rc == 0 else "partial",
                 json.dumps({k: v for k, v in stats.items()
                             if isinstance(v, (int, str))})))
            conn.commit()
            conn.close()
    stats["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats["exit_code"] = rc
    _write_json(Path(args.stats), stats)
    log("---")
    log("summary: " + json.dumps(
        {k: v for k, v in stats.items() if k != "scan_set_no_cik"}, indent=2))
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.backfill",
        description="Historical backfill of analyst-style days from EDGAR. No fan-out.")
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"backfill SQLite DB (default {DEFAULT_DB}); never data/events.db")
    p.add_argument("--event-start", required=True, help="first event date kept, YYYY-MM-DD")
    p.add_argument("--event-end", required=True, help="last event date kept, YYYY-MM-DD")
    p.add_argument("--filings-from", required=True,
                   help="first filing date searched (announcements precede events)")
    p.add_argument("--filings-to", default=None, help="last filing date searched (default today)")
    p.add_argument("--tickers", nargs="*", help="classify only these scan-set tickers")
    p.add_argument("--sweep-only", action="store_true",
                   help="run the EDGAR sweep and stop before the classifier")
    p.add_argument("--dry-run", action="store_true",
                   help="sweep and classify, write no database")
    p.add_argument("--workers", type=int, default=CLASSIFY_WORKERS)
    p.add_argument("--hits-cache", default=DEFAULT_HITS_CACHE)
    p.add_argument("--classified-log", default=DEFAULT_CLASSIFIED_LOG)
    p.add_argument("--stats", default=DEFAULT_STATS)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    from dotenv import load_dotenv
    load_dotenv(".env")
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except BackfillRefused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
