"""Render the analyst-day listing (last year + this year) to a self-contained HTML page.

    python scripts/build_analyst_day_page.py --fetch          # pull the CI DB, then build
    python scripts/build_analyst_day_page.py                  # build from the last fetched snapshot
    python scripts/build_analyst_day_page.py --db PATH --out PATH --today YYYY-MM-DD

Board #439 (JP, 2026-09-17): "an artifact listing every analyst day from this year and
last year for core coverage, portfolio and researching companies." The page is published
as a private Claude Artifact; re-run and re-publish the SAME file path to update it.

Why this script reads a *fetched CI artifact* and never `data/events.db`:
the real events database lives only as the `analyst-days-db` GitHub Actions artifact
(gitignored locally); the local `data/events.db` is an April-2026 dev sandbox with three
rows that would render plausibly and wrongly. `--fetch` selects the newest non-expired
artifact from a SUCCESSFUL master run -- the same choice `monday.yml`'s restore makes --
because the DB is uploaded `if: always()`, so the newest artifact can belong to a failed
run that production will discard.

What the page refuses to imply: completeness. Every stored row lands in exactly one bucket
(shown or counted) and the buckets are asserted disjoint and exhaustive by `events.id`;
the coverage statement (history start, per-year counts, unscanned names) is computed, and a
database whose earliest record is later than `HISTORY_FLOOR` is announced as reset or
pruned (board #456) rather than rendered as if two years of history were present.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

GH_REPO = "jroypeterson/analyst-days"
ARTIFACT_NAME = "analyst-days-db"
SNAPSHOT_DIR = REPO / "data" / "ci_snapshot"
DEFAULT_DB = SNAPSHOT_DIR / "events.db"
PROVENANCE_NAME = "provenance.json"
DEFAULT_OUT = REPO / "exports" / "analyst_day_listing.html"

# The measured start of the current CI database chain (min(first_seen_at) in every
# live weekly artifact 2026-07-13 .. 2026-09-28). An earliest record later than this
# means the artifact was lost and monday.yml started over (board #456), or the earliest
# rows were deleted. Either way the page must say so.
HISTORY_FLOOR = date(2026, 7, 6)
FLOOR_TOLERANCE_DAYS = 1
# Weekly cadence + 1. Older than this and the Monday run did not produce an artifact.
STALE_ARTIFACT_DAYS = 8

ANALYST_DAY_TYPES = ("investor_day", "analyst_day", "rd_day", "capital_markets_day")
TERMINAL_STATUSES = {"cancelled", "superseded"}
CONFIRMED_FAMILY = {"confirmed", "reminded_30", "reminded_7", "day_of", "completed", "historical"}
TYPE_LABEL = {
    "investor_day": "Investor Day",
    "analyst_day": "Analyst Day",
    "rd_day": "R&D Day",
    "capital_markets_day": "Capital Markets Day",
}
SOURCE_LABEL = {
    "8K": "8-K", "IR_PAGE": "IR page", "PRESS_RELEASE": "Press release",
    "TAVILY_HIT": "Web hit", "MANUAL": "Manual",
}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --------------------------------------------------------------------------- fetch

def _gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


def select_artifact(artifacts: list[dict], run_info: Callable[[int], dict]
                    ) -> tuple[Optional[dict], list[dict], Optional[str]]:
    """Newest non-expired master artifact whose run concluded `success`.

    Returns (chosen, skipped_newer_failed, fresh_anchor). `run_info(run_id)` returns
    {"conclusion", "event"} and is a callable so the selection is testable without `gh`.

    - Artifacts from failed runs NEWER than the chosen one are returned separately:
      that a newer run failed is the fact the page prints (the chosen run's
      conclusion is `success` by construction).
    - `fresh_anchor` is the `created_at` of the newest successful **scheduled** run's
      artifact, and is what staleness is measured from. A `workflow_dispatch` run
      (codex r1, 2026-10-04) can be a dry run: `monday.yml` uploads the restored DB
      unchanged under a new timestamp, so its artifact date says nothing about when
      discovery last ran. The run API does not expose the `dry_run` input, so manual
      runs never reset the clock; a real manual run can only make the page look
      older than it is (a false amber), never fresher.
    """
    live = [a for a in artifacts
            if a.get("name") == ARTIFACT_NAME and not a.get("expired")
            and (a.get("workflow_run") or {}).get("head_branch") == "master"]
    live.sort(key=lambda a: a["created_at"], reverse=True)
    skipped: list[dict] = []
    chosen: Optional[dict] = None
    anchor: Optional[str] = None
    for a in live:
        run_id = int(a["workflow_run"]["id"])
        info = run_info(run_id)
        ok = info.get("conclusion") == "success"
        if chosen is None:
            if ok:
                chosen = a
            else:
                skipped.append({"artifact_created_at": a["created_at"], "run_id": run_id,
                                "run_conclusion": info.get("conclusion") or "",
                                "run_url": f"https://github.com/{GH_REPO}/actions/runs/{run_id}"})
        if ok and info.get("event") == "schedule":
            anchor = a["created_at"]
            break
    return chosen, skipped, anchor


def fetch_snapshot(dest_dir: Path = SNAPSHOT_DIR, gh: Callable[[list[str]], str] = _gh,
                   now: Optional[datetime] = None) -> dict:
    """Download the selected artifact into `dest_dir` and write provenance.json."""
    raw = gh(["api", f"repos/{GH_REPO}/actions/artifacts?name={ARTIFACT_NAME}&per_page=100"])
    artifacts = json.loads(raw).get("artifacts", [])

    def run_info(run_id: int) -> dict:
        out = gh(["run", "view", str(run_id), "-R", GH_REPO, "--json", "conclusion,event"])
        return json.loads(out)

    chosen, skipped, anchor = select_artifact(artifacts, run_info)
    if chosen is None:
        raise SystemExit(
            f"No non-expired {ARTIFACT_NAME} artifact from a successful master run "
            f"({len(artifacts)} artifacts listed). Nothing to render.")
    run_id = int(chosen["workflow_run"]["id"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    db = dest_dir / "events.db"
    if db.exists():
        db.unlink()
    gh(["run", "download", str(run_id), "-R", GH_REPO, "-n", ARTIFACT_NAME, "-D", str(dest_dir)])
    if not db.exists():
        raise SystemExit(f"gh run download reported success but {db} is missing")
    now = now or datetime.now(timezone.utc)
    prov = {
        "artifact_id": chosen["id"],
        "artifact_created_at": chosen["created_at"],
        "run_id": run_id,
        "run_url": f"https://github.com/{GH_REPO}/actions/runs/{run_id}",
        "run_conclusion": "success",
        "fetched_at": now.isoformat(timespec="seconds"),
        "skipped_newer_failed": skipped,
        # Staleness is measured from this, not artifact_created_at (see select_artifact).
        "last_scheduled_success_at": anchor,
    }
    (dest_dir / PROVENANCE_NAME).write_text(json.dumps(prov, indent=2), encoding="utf-8")
    return prov


# --------------------------------------------------------------------------- load

def load_events(db_path: Path) -> list[dict]:
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY start_date, ticker")]
    src = {}
    for s in conn.execute("SELECT * FROM event_sources ORDER BY retrieved_at, id"):
        src.setdefault(s["event_id"], []).append(dict(s))
    conn.close()
    for r in rows:
        r["sources"] = src.get(r["id"], [])
    return rows


def _iso_date(ts: Optional[str]) -> Optional[date]:
    """`2026-07-06T15:54:22+00:00` (what events_repo writes) -> date. Never a prefix slice."""
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()


def bucket_events(rows: list[dict], years: tuple[int, int]) -> dict[str, list[dict]]:
    """Exactly one bucket per row; precedence is the order of the branches."""
    b: dict[str, list[dict]] = {k: [] for k in
                                ("retired", "conference", "unknown_type", "undated",
                                 "listed", "outside_years")}
    for r in rows:
        if r["status"] in TERMINAL_STATUSES:
            b["retired"].append(r)
        elif r["event_type"] == "conference":
            b["conference"].append(r)
        elif r["event_type"] not in ANALYST_DAY_TYPES:
            b["unknown_type"].append(r)
        elif r["start_date"] is None or r["date_imprecise"]:
            b["undated"].append(r)
        elif int(r["start_date"][:4]) in years:
            b["listed"].append(r)
        else:
            b["outside_years"].append(r)
    assert_partition(b, rows)
    return b


def assert_partition(buckets: dict[str, list[dict]], rows: list[dict]) -> None:
    """Buckets are pairwise disjoint by `events.id` and their union is every row.

    Separate from `bucket_events` so the check is exercisable on its own: the
    if/elif chain above cannot drop a row by construction, so only a direct call can
    prove this guard still bites.
    """
    union: set[int] = set()
    for name, v in buckets.items():
        s = {x["id"] for x in v}
        if union & s:
            raise AssertionError(f"bucket overlap on ids {sorted(union & s)} ({name})")
        union |= s
    missing = {r["id"] for r in rows} - union
    if missing:
        raise AssertionError(f"buckets do not cover events.id {sorted(missing)}")


# --------------------------------------------------------------------------- universe

def _read_json_tickers(cm_root: Path, name: str) -> dict[str, dict]:
    p = cm_root / "exports" / name
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8-sig")) or {}


def load_universe(cm_root: Path) -> dict:
    """Membership tags + CIKs from Coverage Manager, and the set discovery scans."""
    from src.universe import load_core_watchlist

    portfolio = _read_json_tickers(cm_root, "portfolio.json")
    researching = _read_json_tickers(cm_root, "researching.json")
    ffi = _read_json_tickers(cm_root, "following_for_interest.json")
    core: dict[str, dict] = {}
    with (cm_root / "exports" / "universe.csv").open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if (row.get("Core") or "").strip().upper() == "Y":
                core[row["Ticker"].strip()] = row
    scanned = {t.ticker for t in load_core_watchlist(cm_root)}

    tags: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    ciks: dict[str, str] = {}
    for label, table in (("Portfolio", portfolio), ("Researching", researching),
                         ("Following", ffi)):
        for t, e in table.items():
            tags.setdefault(t, set()).add(label)
            names.setdefault(t, e.get("Company Name") or e.get("name") or "")
            if e.get("CIK"):
                ciks.setdefault(t, str(e["CIK"]))
    for t, row in core.items():
        tags.setdefault(t, set()).add("Core")
        names.setdefault(t, row.get("Company Name") or "")
        if row.get("CIK"):
            ciks.setdefault(t, str(row["CIK"]))
    return {"tags": tags, "names": names, "ciks": ciks, "scanned": scanned,
            "portfolio": set(portfolio), "researching": set(researching),
            "ffi": set(ffi), "core": set(core)}


def edgar_fts_url(cik: str, start: date, end: date) -> str:
    q = quote('"investor day" OR "analyst day" OR "capital markets day"')
    return (f"https://www.sec.gov/edgar/search/#/q={q}&dateRange=custom"
            f"&startdt={start.isoformat()}&enddt={end.isoformat()}"
            f"&ciks={str(cik).strip().zfill(10)}")


# --------------------------------------------------------------------------- render

def _esc(s) -> str:
    return (str("" if s is None else s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _when(r: dict) -> str:
    sd = r["start_date"]
    y, m, d = sd.split("-")
    out = f"{MONTHS[int(m) - 1]} {int(d)}"
    if r.get("end_date") and r["end_date"] != sd:
        ey, em, ed = r["end_date"].split("-")
        out += f"-{int(ed)}" if em == m else f" - {MONTHS[int(em) - 1]} {int(ed)}"
    return out


def _tier(r: dict) -> tuple[str, str]:
    if r["status"] in CONFIRMED_FAMILY:
        return "confirmed", "Confirmed"
    return "lead", "Lead"


def _tags_html(ticker: str, uni: dict) -> str:
    tags = uni["tags"].get(ticker, set())
    order = ("Portfolio", "Researching", "Core", "Following")
    if not tags:
        return '<span class="tag none">not on any list today</span>'
    if tags == {"Following"}:
        return '<span class="tag ffi">Following only</span>'
    return "".join(f'<span class="tag {t.lower()}">{t}</span>' for t in order if t in tags)


def _safe_url(url) -> Optional[str]:
    """http(s) only. Source URLs are classifier output over web search results, so a
    `javascript:` / `data:` URL is untrusted input that must never become an href
    (codex r2, 2026-10-04)."""
    u = str(url or "").strip()
    return u if u.lower().startswith(("https://", "http://")) else None


def _sources_html(r: dict) -> str:
    parts = []
    for s in r["sources"]:
        label = SOURCE_LABEL.get(s["source_type"], s["source_type"])
        tip = _esc((s.get("source_excerpt") or "").strip())
        url = _safe_url(s.get("source_url"))
        if url:
            parts.append(f'<a href="{_esc(url)}" target="_blank" '
                         f'rel="noopener" title="{tip}">{_esc(label)}</a>')
        elif s.get("source_url"):
            parts.append(f'<span class="nolink" title="{tip}">{_esc(label)} (non-web link withheld: '
                         f'{_esc(str(s["source_url"])[:60])})</span>')
        else:
            parts.append(f'<span class="nolink" title="{tip}">{_esc(label)} (no link recorded)</span>')
    return " · ".join(parts) if parts else '<span class="nolink">no source rows</span>'


def _event_row(r: dict, uni: dict, today: date) -> str:
    cls, tier_label = _tier(r)
    past = r["start_date"] is not None and date.fromisoformat(r["start_date"]) < today
    when = _when(r) if r["start_date"] and not r["date_imprecise"] else \
        (r.get("imprecise_hint") or "date not yet known")
    conf = f'{r["confidence"]:.2f}' if r.get("confidence") is not None else "-"
    return (
        f'<tr class="{cls}{" past" if past else ""}" data-event-id="{r["id"]}">'
        f'<td class="d">{_esc(when)}</td>'
        f'<td class="t"><b>{_esc(r["ticker"])}</b><span class="co">{_esc(r.get("company_name"))}</span></td>'
        f'<td>{_esc(TYPE_LABEL.get(r["event_type"], r["event_type"]))}</td>'
        f'<td><span class="tier {cls}">{tier_label}</span>'
        f'<span class="conf">{conf}</span></td>'
        f'<td class="tags">{_tags_html(r["ticker"], uni)}</td>'
        f'<td class="src">{_sources_html(r)}</td>'
        "</tr>")


def _year_section(year: int, rows: list[dict], uni: dict, today: date, history_start: Optional[date]) -> str:
    rows = sorted(rows, key=lambda r: (r["start_date"], r["ticker"]))
    n_conf = sum(1 for r in rows if _tier(r)[0] == "confirmed")
    if rows:
        body = "".join(_event_row(r, uni, today) for r in rows)
        table = (TABLE_HEAD + body + "</tbody></table>")
    else:
        table = ""
    if not rows:
        reason = ("none recorded. Discovery began "
                  f"{history_start.isoformat() if history_start else 'after this year'} and "
                  "the classifier drops past events, so this year was not searched. "
                  "A gap, not an absence.")
        if year == today.year:
            reason = "none recorded yet."
        empty = f'<p class="empty">{_esc(reason)}</p>'
    else:
        empty = ""
    return (
        f'<section class="year"><div class="yh"><h2>{year}</h2>'
        f'<span class="ym">{len(rows)} recorded · {n_conf} confirmed · {len(rows) - n_conf} leads</span></div>'
        f"{table}{empty}</section>")


def _undated_section(rows: list[dict], uni: dict, today: date) -> str:
    if not rows:
        return ""
    rows = sorted(rows, key=lambda r: (r["ticker"], r["id"]))
    body = "".join(_event_row(r, uni, today) for r in rows)
    return (
        '<section class="year"><div class="yh"><h2>Date not yet known</h2>'
        f'<span class="ym">{len(rows)} recorded · announced or hinted, no precise date</span></div>'
        f"{TABLE_HEAD}{body}</tbody></table></section>")


def _coverage_table(uni: dict, counted_by_ticker: dict[str, int], today: date, years: tuple[int, int]) -> str:
    pr = sorted(uni["portfolio"] | uni["researching"])
    rows = []
    fts_start = date(years[0], 1, 1)
    for t in pr:
        n = counted_by_ticker.get(t, 0)
        scanned = t in uni["scanned"]
        if not scanned:
            state, cls = "not scanned", "no"
        elif n:
            state, cls = f"{n} recorded", "yes"
        else:
            state, cls = "scanned, none recorded", "zero"
        check = ""
        if cls != "yes":
            cik = uni["ciks"].get(t)
            if cik:
                check = (f'<a href="{_esc(edgar_fts_url(cik, fts_start, today))}" target="_blank" '
                         f'rel="noopener">EDGAR full-text search</a>')
            else:
                check = '<span class="nolink">no CIK in Coverage Manager, no EDGAR check available</span>'
        rows.append(
            f'<tr class="cov-{cls}"><td><b>{_esc(t)}</b><span class="co">{_esc(uni["names"].get(t, ""))}</span></td>'
            f'<td class="tags">{_tags_html(t, uni)}</td>'
            f'<td><span class="state {cls}">{state}</span></td><td class="src">{check}</td></tr>')
    return ('<table class="cov"><thead><tr><th>Company</th><th>Lists</th>'
            f'<th>Analyst days {years[0]}-{years[1]}</th><th>Manual check</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table>")


TABLE_HEAD = ('<table class="ev"><thead><tr><th>Date</th><th>Company</th><th>Event</th>'
              '<th>Tier · conf.</th><th>Lists</th><th>Sources</th></tr></thead><tbody>')


def build(rows: list[dict], uni: dict, prov: Optional[dict], today: date,
          years: Optional[tuple[int, int]] = None) -> str:
    years = years or (today.year - 1, today.year)
    b = bucket_events(rows, years)
    shown = b["listed"] + b["undated"]

    firsts = [_iso_date(r["first_seen_at"]) for r in rows if r.get("first_seen_at")]
    lasts = [_iso_date(r["last_seen_at"]) for r in rows if r.get("last_seen_at")]
    history_start = min(firsts) if firsts else None
    last_touch = max(lasts) if lasts else None

    banners = []
    live_banners: list[str] = []
    if not rows or history_start is None or history_start > HISTORY_FLOOR + timedelta(days=FLOOR_TOLERANCE_DAYS):
        earliest = history_start.isoformat() if history_start else "none (empty database)"
        banners.append(
            '<div class="banner red" data-banner="reset"><b>History is missing.</b> Earliest record is '
            f'{_esc(earliest)}, expected on or before {HISTORY_FLOOR.isoformat()}: the database was '
            'reset (board #456) or its earliest rows were deleted. Events recorded before '
            f'{_esc(earliest)} are missing from this page.</div>')
    if prov is None:
        banners.append(
            '<div class="banner red" data-banner="unverified"><b>Unverified source database.</b> '
            'This page was built from a database with no fetch provenance, so its origin and '
            'freshness are unknown. Rebuild with <code>--fetch</code>.</div>')
    else:
        anchor = _iso_date(prov.get("last_scheduled_success_at"))
        if anchor is None:
            banners.append(
                '<div class="banner amber" data-banner="no-scheduled"><b>Freshness unknown.</b> No '
                'successful scheduled weekly run was found among the live database artifacts, so '
                'when discovery last ran cannot be stated.</div>')
        else:
            age = (today - anchor).days
            if age > STALE_ARTIFACT_DAYS:
                banners.append(
                    f'<div class="banner amber" data-banner="stale"><b>Stale.</b> The last successful '
                    f'scheduled run was {age} days before this page was built ({anchor.isoformat()}).</div>')
            # The page is static and outlives its build (codex r1): the same check is
            # re-run against the viewer's clock by the inline script, so a page opened
            # three weeks later says so even if nobody rebuilds it.
            live_banners.append(
                f'<div class="banner amber" data-banner="stale-live" data-anchor="{anchor.isoformat()}" '
                f'data-stale-days="{STALE_ARTIFACT_DAYS}" hidden><b>Stale.</b> The last successful '
                f'scheduled run was <span data-age></span> days ago ({anchor.isoformat()}). '
                'Rebuild with <code>--fetch</code> and re-publish.</div>')
        if prov.get("skipped_newer_failed"):
            newest = prov["skipped_newer_failed"][0]
            banners.append(
                '<div class="banner amber" data-banner="newer-failed"><b>Newest run failed.</b> '
                f'The run of {_esc(newest["artifact_created_at"][:10])} did not succeed '
                f'(<a href="{_esc(_safe_url(newest.get("run_url")) or "#")}" target="_blank" rel="noopener">run</a>); '
                f'showing the last successful database from {_esc(prov["artifact_created_at"][:10])}.</div>')

    n_this = sum(1 for r in b["listed"] if int(r["start_date"][:4]) == years[1])
    n_last = len(b["listed"]) - n_this
    counted_by_ticker: dict[str, int] = {}
    for r in shown:
        counted_by_ticker[r["ticker"]] = counted_by_ticker.get(r["ticker"], 0) + 1

    pr = uni["portfolio"] | uni["researching"]
    pr_unscanned = sorted(pr - uni["scanned"])
    core_unscanned = sorted(uni["core"] - uni["scanned"])
    headline = (f'<p class="headline"><b>{len(shown)}</b> analyst-day rows recorded · '
                f'<b>{n_this}</b> dated {years[1]} · <b>{n_last}</b> dated {years[0]} · '
                f'<b>{len(b["undated"])}</b> date not yet known</p>')

    last_year_sentence = (f"{n_last} analyst-day rows recorded for {years[0]}."
                          if n_last else
                          f"{years[0]}: none recorded. Not searched, so this is a gap, not an absence.")
    coverage = (
        '<section class="cov-note"><h2>What this page can and cannot show</h2><ul>'
        f'<li>Recorded since <b>{_esc(history_start.isoformat() if history_start else "-")}</b>. '
        'Discovery looks forward (8-K/6-K lookback of 14 days, web search for the current year) and the '
        'classifier is instructed to drop past events, so earlier years are effectively unsearched. '
        f'{_esc(last_year_sentence)}</li>'
        f'<li>Discovery scans <b>{len(uni["scanned"])}</b> tickers (watchlist Core=Y plus Following for '
        f'Interest). <b>{len(pr_unscanned)}</b> of your {len(pr)} Portfolio/Researching names and '
        f'<b>{len(core_unscanned)}</b> of your {len(uni["core"])} Core-coverage names are not scanned, so no '
        'event can appear for them here.'
        + (f' Unscanned Portfolio/Researching: {_esc(", ".join(pr_unscanned))}.' if pr_unscanned else "")
        + '</li>'
        '<li><span class="tier confirmed">Confirmed</span> rows passed the date-grounding gate and have an '
        'authoritative source (8-K, IR page, press release). <span class="tier lead">Lead</span> rows are '
        'discovered or tentative: they include low-confidence and wrong-issuer matches (a web hit that names '
        'the ticker but belongs to another company). Check the source link before relying on one.</li>'
        f'<li>List membership is as of {today.isoformat()} from Coverage Manager exports.</li>'
        '</ul></section>')

    def tick_list(rs):
        return _esc(", ".join(sorted({r["ticker"] for r in rs})))
    outside = _esc("; ".join(f'{r["ticker"]} {r["start_date"]}' for r in
                             sorted(b["outside_years"], key=lambda r: r["start_date"])))
    ledger = (
        '<section class="ledger"><h2>Every stored row, accounted for</h2>'
        f'<table class="led"><tbody>'
        f'<tr><td>Total rows in the events database</td><td class="n">{len(rows)}</td><td></td></tr>'
        f'<tr><td>Shown: analyst days dated {years[0]}-{years[1]}</td><td class="n">{len(b["listed"])}</td><td></td></tr>'
        f'<tr><td>Shown: analyst days, date not yet known</td><td class="n">{len(b["undated"])}</td><td></td></tr>'
        f'<tr><td>Counted only: conference appearances (a company presenting at a conference, not an analyst day)</td>'
        f'<td class="n">{len(b["conference"])}</td><td>{tick_list(b["conference"])}</td></tr>'
        f'<tr><td>Counted only: retired (cancelled or superseded)</td><td class="n">{len(b["retired"])}</td>'
        f'<td>{tick_list(b["retired"])}</td></tr>'
        f'<tr><td>Counted only: analyst days outside {years[0]}-{years[1]}</td><td class="n">{len(b["outside_years"])}</td>'
        f'<td>{outside}</td></tr>'
        + (f'<tr><td>Counted only: unknown event type</td><td class="n">{len(b["unknown_type"])}</td>'
           f'<td>{tick_list(b["unknown_type"])}</td></tr>' if b["unknown_type"] else "")
        + '</tbody></table></section>')

    if prov:
        skipped_n = len(prov.get("skipped_newer_failed") or [])
        prov_html = (
            f'Source: GitHub Actions artifact <code>{ARTIFACT_NAME}</code> #{_esc(prov["artifact_id"])}, '
            f'created {_esc(prov["artifact_created_at"])}, from '
            f'<a href="{_esc(_safe_url(prov.get("run_url")) or "#")}" target="_blank" rel="noopener">run {_esc(prov["run_id"])}</a> '
            f'(conclusion: {_esc(prov.get("run_conclusion", "?"))}); fetched {_esc(prov["fetched_at"])}.'
            + (f' {skipped_n} newer artifact(s) from failed runs skipped.' if skipped_n else "")
            + f' Last successful scheduled run: {_esc(prov.get("last_scheduled_success_at") or "none found")}.')
    else:
        prov_html = "Source: an explicitly supplied database with no provenance record."
    footer = (
        '<footer>'
        f'<span>{prov_html}</span>'
        f'<span>Last time discovery touched a row: {_esc(last_touch.isoformat() if last_touch else "-")}. '
        f'Page built {today.isoformat()} by <code>scripts/build_analyst_day_page.py</code>.</span>'
        '<span>Every row above carries its <code>events.id</code> and every stored source link; the page is a '
        'projection of the database, never a hand-maintained copy.</span>'
        '</footer>')

    years_html = (_year_section(years[1], [r for r in b["listed"] if int(r["start_date"][:4]) == years[1]],
                                uni, today, history_start)
                  + _year_section(years[0], [r for r in b["listed"] if int(r["start_date"][:4]) == years[0]],
                                  uni, today, history_start)
                  + _undated_section(b["undated"], uni, today))

    return (PAGE
            .replace("%%BANNERS%%", "".join(banners + live_banners))
            .replace("%%HEADLINE%%", headline)
            .replace("%%SPAN%%", f"{years[0]} & {years[1]}")
            .replace("%%YEARS%%", years_html)
            .replace("%%COVERAGE%%", coverage)
            .replace("%%COVTABLE%%", _coverage_table(uni, counted_by_ticker, today, years))
            .replace("%%PR_N%%", str(len(pr)))
            .replace("%%LEDGER%%", ledger)
            .replace("%%FOOTER%%", footer))


PAGE = """<title>Analyst Day Listing</title>
<style>
  /* Same token family as the lane's conference calendar, so the two pages read as one
     system. Layout: one column, headline count first, then the two years, then the
     per-company coverage table, then the ledger. */
  :root {
    --ground:#F4F7F5; --surface:#FFFFFF; --surface-2:#EBF1EE;
    --ink:#0F1618; --ink-2:#3D4B4C; --ink-3:#6B7B79;
    --rule:#D7E1DD; --rule-2:#E8EEEB;
    --accent:#0B6A56; --accent-ink:#0B6A56; --accent-soft:#DCEBE5;
    --amber:#855312; --amber-soft:#F3E8D6;
    --red:#9B2C2C; --red-soft:#F6E0E0;
    --shadow:0 1px 2px rgba(15,22,24,.05), 0 6px 18px -12px rgba(15,22,24,.22);
    --display:Georgia,"Iowan Old Style","Palatino Linotype",Palatino,serif;
    --body:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --ground:#0B1113; --surface:#121A1C; --surface-2:#0F1719;
      --ink:#E4ECE9; --ink-2:#B3C3BF; --ink-3:#849692;
      --rule:#223032; --rule-2:#1A2527;
      --accent:#52C0A0; --accent-ink:#7FD3B9; --accent-soft:#12332C;
      --amber:#D9A860; --amber-soft:#2E2413;
      --red:#E08787; --red-soft:#3A1A1A;
      --shadow:0 1px 2px rgba(0,0,0,.4), 0 6px 18px -12px rgba(0,0,0,.8);
      color-scheme:dark;
    }
  }
  :root[data-theme="dark"] {
    --ground:#0B1113; --surface:#121A1C; --surface-2:#0F1719;
    --ink:#E4ECE9; --ink-2:#B3C3BF; --ink-3:#849692;
    --rule:#223032; --rule-2:#1A2527;
    --accent:#52C0A0; --accent-ink:#7FD3B9; --accent-soft:#12332C;
    --amber:#D9A860; --amber-soft:#2E2413;
    --red:#E08787; --red-soft:#3A1A1A;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 6px 18px -12px rgba(0,0,0,.8);
    color-scheme:dark;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--ground); color:var(--ink); font-family:var(--body);
         font-size:15px; line-height:1.45; -webkit-font-smoothing:antialiased; }
  .wrap { max-width:72rem; margin:0 auto; padding-block:clamp(1.5rem,3vw,2.5rem) 3rem;
          padding-inline:clamp(1rem,2.5vw,1.75rem); display:flex; flex-direction:column; gap:1.4rem; }
  header.top { display:flex; flex-direction:column; gap:.35rem; }
  .eyebrow { font-family:var(--mono); font-size:.66rem; letter-spacing:.14em; text-transform:uppercase;
             color:var(--ink-3); margin:0; }
  h1 { font-family:var(--display); font-weight:400; margin:0; font-size:clamp(1.6rem,3.4vw,2.3rem);
       line-height:1.1; letter-spacing:-.015em; text-wrap:balance; }
  h1 em { font-style:italic; color:var(--accent-ink); }
  .headline { margin:.3rem 0 0; font-family:var(--mono); font-size:.8rem; color:var(--ink-2);
              font-variant-numeric:tabular-nums; }
  .headline b { color:var(--ink); font-size:1rem; }
  .sub { margin:.15rem 0 0; color:var(--ink-2); font-size:.88rem; max-width:72ch; }
  .banner { padding:.7rem .9rem; border-radius:6px; font-size:.86rem; border-left:4px solid; }
  .banner.red { background:var(--red-soft); border-color:var(--red); color:var(--ink); }
  .banner.amber { background:var(--amber-soft); border-color:var(--amber); color:var(--ink); }
  .banner a { color:inherit; }
  h2 { font-family:var(--display); font-weight:400; margin:0; font-size:1.35rem; letter-spacing:-.01em; }
  .year { display:flex; flex-direction:column; gap:.55rem; }
  .yh { display:flex; align-items:baseline; gap:.7rem; flex-wrap:wrap; }
  .ym { font-family:var(--mono); font-size:.7rem; color:var(--ink-3); font-variant-numeric:tabular-nums; }
  .tablewrap, table { max-width:100%; }
  table { width:100%; border-collapse:collapse; background:var(--surface); border-radius:8px;
          box-shadow:var(--shadow); font-size:.83rem; overflow:hidden; }
  th { text-align:left; font-family:var(--mono); font-size:.62rem; letter-spacing:.1em; text-transform:uppercase;
       color:var(--ink-3); padding:.55rem .7rem; border-bottom:1px solid var(--rule); font-weight:600; }
  td { padding:.5rem .7rem; border-bottom:1px solid var(--rule-2); vertical-align:top; }
  tr:last-child td { border-bottom:0; }
  td.d { font-family:var(--mono); font-size:.76rem; white-space:nowrap; font-variant-numeric:tabular-nums; }
  td.n { font-family:var(--mono); text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  td.t b { display:block; }
  .co { display:block; color:var(--ink-3); font-size:.74rem; }
  tr.past td.d, tr.past td.t b { color:var(--ink-3); }
  tr.lead td { color:var(--ink-2); }
  .tier { font-family:var(--mono); font-size:.6rem; letter-spacing:.06em; text-transform:uppercase;
          padding:.08rem .35rem; border-radius:3px; white-space:nowrap; }
  .tier.confirmed { background:var(--accent-soft); color:var(--accent-ink); }
  .tier.lead { background:var(--amber-soft); color:var(--amber); }
  .conf { font-family:var(--mono); font-size:.7rem; color:var(--ink-3); margin-left:.35rem; }
  .tag { display:inline-block; font-family:var(--mono); font-size:.6rem; letter-spacing:.05em; text-transform:uppercase;
         padding:.06rem .32rem; border-radius:3px; margin:0 .2rem .15rem 0; border:1px solid var(--rule); color:var(--ink-2); }
  .tag.portfolio { border-color:var(--accent); color:var(--accent-ink); }
  .tag.none, .tag.ffi { color:var(--ink-3); border-style:dashed; }
  td.src a { color:var(--accent-ink); text-decoration:none; border-bottom:1px solid var(--rule); }
  td.src a:hover, td.src a:focus-visible { border-bottom-color:var(--accent); }
  td.src a:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  .nolink { color:var(--ink-3); font-style:italic; }
  .state { font-family:var(--mono); font-size:.72rem; }
  .state.no { color:var(--red); }
  .state.zero { color:var(--amber); }
  .state.yes { color:var(--accent-ink); }
  p.empty { margin:0; padding:.8rem .9rem; background:var(--surface); border-radius:8px; box-shadow:var(--shadow);
            font-size:.86rem; color:var(--ink-2); }
  .cov-note { background:var(--surface-2); border-radius:8px; padding:.9rem 1.1rem; display:flex; flex-direction:column; gap:.5rem; }
  .cov-note h2 { font-size:1.1rem; }
  .cov-note ul { margin:0; padding-left:1.1rem; display:flex; flex-direction:column; gap:.35rem; font-size:.86rem; color:var(--ink-2); }
  .cov-note b { color:var(--ink); }
  details { background:var(--surface); border-radius:8px; box-shadow:var(--shadow); }
  summary { cursor:pointer; padding:.7rem .9rem; font-family:var(--display); font-size:1.1rem; }
  summary:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  details table { box-shadow:none; border-radius:0; }
  .ledger { display:flex; flex-direction:column; gap:.55rem; }
  .ledger h2 { font-size:1.1rem; }
  table.led td:first-child { color:var(--ink-2); }
  table.led td:last-child { color:var(--ink-3); font-family:var(--mono); font-size:.72rem; }
  footer { border-top:1px solid var(--rule); padding-top:.85rem; font-size:.73rem; color:var(--ink-3);
           display:flex; flex-direction:column; gap:.25rem; }
  footer code, .banner code, .cov-note code { font-family:var(--mono); font-size:.71rem; color:var(--ink-2); }
  footer a { color:var(--ink-2); }
  .tablewrap { overflow-x:auto; min-width:0; }
  @media (max-width:40rem) {
    .tag { display:block; width:max-content; }
    td, th { padding:.45rem .5rem; }
  }
</style>

<div class="wrap">
  <header class="top">
    <p class="eyebrow">Analyst Days &middot; recorded events</p>
    <h1>Analyst days on the book, <em>%%SPAN%%</em></h1>
    %%HEADLINE%%
    <p class="sub">Investor, analyst, R&amp;D and capital-markets days recorded by the weekly
      discovery run for Portfolio, Researching, Core-coverage and Following-for-Interest
      companies. Each row links to the sources it was built from.</p>
  </header>

  %%BANNERS%%

  %%YEARS%%

  %%COVERAGE%%

  <details>
    <summary>Coverage by company: %%PR_N%% Portfolio and Researching names</summary>
    <div class="tablewrap">%%COVTABLE%%</div>
  </details>

  %%LEDGER%%

  %%FOOTER%%
</div>

<script>
/* Re-run the build-time staleness check against the viewer's clock: the page is a
   static file that is opened long after it is built. Pure date arithmetic in UTC. */
(function () {
  var el = document.querySelector('[data-banner="stale-live"]');
  if (!el) { return; }
  var a = el.getAttribute("data-anchor").split("-");
  var anchor = Date.UTC(+a[0], +a[1] - 1, +a[2]);
  var now = new Date();
  var today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  var age = Math.round((today - anchor) / 86400000);
  if (age > +el.getAttribute("data-stale-days")) {
    el.querySelector("[data-age]").textContent = String(age);
    var built = document.querySelector('[data-banner="stale"]');
    if (built) { built.hidden = true; }
    el.hidden = false;
  }
})();
</script>
"""


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    load_dotenv(REPO / ".env")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fetch", action="store_true",
                    help="Download the newest successful-run CI database first (needs gh).")
    ap.add_argument("--db", type=Path, default=None,
                    help=f"Explicit events.db (default: {DEFAULT_DB}; never data/events.db).")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cm", type=Path, default=None, help="Coverage Manager root (default: COVERAGE_MANAGER_PATH)")
    ap.add_argument("--today", default=None, help="Override today (YYYY-MM-DD) for testing.")
    args = ap.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else date.today()

    if args.fetch:
        prov = fetch_snapshot()
        db_path = DEFAULT_DB
        print(f"fetched artifact {prov['artifact_id']} ({prov['artifact_created_at']}) "
              f"from run {prov['run_id']}; {len(prov['skipped_newer_failed'])} newer failed run(s) skipped")
    elif args.db is not None:
        db_path = args.db
        prov_path = db_path.parent / PROVENANCE_NAME
        prov = json.loads(prov_path.read_text(encoding="utf-8")) if prov_path.exists() else None
    else:
        db_path = DEFAULT_DB
        if not db_path.exists():
            print(f"No fetched snapshot at {db_path}. Run with --fetch (needs gh). "
                  "This script never reads data/events.db, which is a local dev sandbox.",
                  file=sys.stderr)
            return 2
        prov = json.loads((SNAPSHOT_DIR / PROVENANCE_NAME).read_text(encoding="utf-8"))

    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    cm_root = args.cm or os.environ.get("COVERAGE_MANAGER_PATH")
    if not cm_root:
        print("COVERAGE_MANAGER_PATH not set and --cm not given", file=sys.stderr)
        return 2
    uni = load_universe(Path(cm_root))
    rows = load_events(db_path)
    years = (today.year - 1, today.year)
    html = build(rows, uni, prov, today, years)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    b = bucket_events(rows, years)
    print(f"{len(rows)} rows read: {len(b['listed'])} analyst days dated {years[0]}-{years[1]}, "
          f"{len(b['undated'])} undated shown; {len(b['conference'])} conference / "
          f"{len(b['retired'])} retired / {len(b['outside_years'])} other-year counted -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
