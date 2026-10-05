"""#439 analyst-day listing page: a projection that must never imply completeness.

Fixtures are built through `init_db` + `events_repo.upsert_event` with a frozen clock,
so the row shape (the `+00:00` timestamps, `date_grounded` default, status promotion)
is production's, not a hand-written approximation of it.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import shutil

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.state import events_repo  # noqa: E402
from src.state.events_repo import CandidateEvent, CandidateSource  # noqa: E402
from src.state.schema import init_db  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "build_analyst_day_page", REPO / "scripts" / "build_analyst_day_page.py")
page = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(page)

TODAY = date(2026, 10, 4)
FLOOR = page.HISTORY_FLOOR


# ------------------------------------------------------------------ fixtures

def _frozen(monkeypatch, iso: str):
    monkeypatch.setattr(events_repo, "_utcnow", lambda: iso)


def _add(conn, monkeypatch, ticker, etype, start, *, seen="2026-07-06T15:54:22+00:00",
         imprecise=False, hint=None, conf=0.9, grounded=True, src="PRESS_RELEASE",
         url="https://example.com/pr"):
    _frozen(monkeypatch, seen)
    cand = CandidateEvent(ticker=ticker, company_name=f"{ticker} Inc", event_type=etype,
                          start_date=start, date_imprecise=imprecise, imprecise_hint=hint,
                          confidence=conf, date_grounded=grounded,
                          sources=[CandidateSource(source_type=src, source_url=url,
                                                   source_excerpt="says " + (start or "soon"))])
    # Promotion happens on the discovery day, so `today` for the upsert is the seen date
    # (a precise date already past `today` is never confirmed -- the past-date backstop).
    eid, status, _ = events_repo.upsert_event(conn, cand, today_iso=seen[:10])
    conn.commit()
    return eid, status


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Production-shaped DB: confirmed + lead + undated + dated-imprecise + conference
    + retired + other-year, first_seen on the floor day."""
    path = tmp_path / "events.db"
    conn = init_db(path)
    ids = {}
    ids["conf26"] = _add(conn, monkeypatch, "CI", "investor_day", "2026-09-30")[0]
    ids["lead26"] = _add(conn, monkeypatch, "ADI", "investor_day", "2026-07-14", conf=0.62,
                         src="TAVILY_HIT", seen="2026-07-13T15:09:58+00:00")[0]
    ids["lead25"] = _add(conn, monkeypatch, "FDX", "investor_day", "2025-11-03", conf=0.82,
                         src="IR_PAGE", seen="2026-08-31T19:11:41+00:00")[0]
    ids["undated"] = _add(conn, monkeypatch, "UPS", "analyst_day", None, imprecise=True,
                          conf=0.45, src="IR_PAGE", url=None)[0]
    # dated-but-imprecise: the classifier may emit both; _supersede_imprecise_null_siblings
    # retires only NULL-start siblings, so this row persists in production.
    ids["dated_imprecise"] = _add(conn, monkeypatch, "ROIV", "investor_day", "2026-07-01",
                                  imprecise=True, hint="Q3 2026", conf=0.4, src="TAVILY_HIT")[0]
    ids["conference"] = _add(conn, monkeypatch, "LLY", "conference", "2026-10-29", conf=0.8)[0]
    ids["retired"] = _add(conn, monkeypatch, "WAY", "investor_day", "2026-03-03")[0]
    events_repo.retire_event(conn, ids["retired"], "cancelled", "called off")
    ids["outside"] = _add(conn, monkeypatch, "JPM", "investor_day", "2027-02-22")[0]
    conn.commit()
    conn.close()
    return path, ids


@pytest.fixture
def cm(tmp_path):
    """Minimal Coverage Manager exports dir that `load_core_watchlist` accepts."""
    root = tmp_path / "cm"
    ex = root / "exports"
    ex.mkdir(parents=True)
    (ex / "watchlist_status.json").write_text(json.dumps({"schema_version": 4}), encoding="utf-8")
    cols = ["Ticker", "Company Name", "Sector (JP)", "Subsector (JP)", "Sub-subsector (JP)",
            "YF Sector", "YF Industry", "CIK", "Website", "Country (HQ)", "ISIN", "Core"]

    def row(t, core="Y", cik="0000001"):
        return {c: "" for c in cols} | {"Ticker": t, "Company Name": f"{t} Inc", "CIK": cik, "Core": core}

    # watchlist = Portfolio + Researching. CI, ADI, FDX, UPS, WAY, JPM scanned; ROIV via FFI;
    # NOSCAN is Researching but Core=N (not scanned); 2715.HK has no CIK.
    wl = [row("CI"), row("ADI"), row("FDX"), row("UPS"), row("WAY"), row("JPM"),
          row("NOSCAN", core="N"), row("2715.HK", core="N", cik="")]
    with (ex / "watchlist.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(wl)
    with (ex / "universe.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(wl + [row("COREONLY")])
    (ex / "portfolio.json").write_text(json.dumps(
        {t: {"name": f"{t} Inc", "CIK": "0000001"} for t in ("CI", "ADI", "WAY")}), encoding="utf-8")
    (ex / "researching.json").write_text(json.dumps(
        {t: {"name": f"{t} Inc", "CIK": "0000001"} for t in ("FDX", "UPS", "JPM", "NOSCAN")}
        | {"2715.HK": {"name": "HK Co"}}), encoding="utf-8")
    (ex / "following_for_interest.json").write_text(json.dumps(
        {"ROIV": {"name": "Roivant", "CIK": "0000002"}, "LLY": {"name": "Lilly", "CIK": "0000003"}}),
        encoding="utf-8")
    return root


def _prov(created="2026-09-28T19:45:17Z", skipped=None, scheduled="same"):
    return {"artifact_id": 1, "artifact_created_at": created, "run_id": 7,
            "run_url": "https://github.com/x/y/actions/runs/7", "run_conclusion": "success",
            "fetched_at": "2026-10-04T00:00:00+00:00", "skipped_newer_failed": skipped or [],
            "last_scheduled_success_at": created if scheduled == "same" else scheduled}


def _build(db, cm, prov=_prov(), today=TODAY):
    rows = page.load_events(db[0])
    uni = page.load_universe(cm)
    return page.build(rows, uni, prov, today), rows


def _banners(html):
    # Only rendered banner elements, not the selector strings inside the page script;
    # a `hidden` banner (the live re-check) is not shown at rest.
    return [n for n, rest in re.findall(r'<div class="banner [a-z]+" data-banner="([-\w]+)"([^>]*)>', html)
            if " hidden" not in rest]


# ------------------------------------------------------------------ buckets

def test_every_row_lands_in_exactly_one_bucket_with_the_stated_precedence(db):
    path, ids = db
    rows = page.load_events(path)
    b = page.bucket_events(rows, (2025, 2026))
    got = {k: {r["id"] for r in v} for k, v in b.items()}
    assert got["retired"] == {ids["retired"]}          # terminal beats "dated 2026"
    assert got["conference"] == {ids["conference"]}
    assert got["undated"] == {ids["undated"], ids["dated_imprecise"]}  # imprecise beats its date
    assert got["listed"] == {ids["conf26"], ids["lead26"], ids["lead25"]}
    assert got["outside_years"] == {ids["outside"]}
    assert got["unknown_type"] == set()
    all_ids = [i for s in got.values() for i in s]
    assert len(all_ids) == len(set(all_ids)) == len(rows)


def test_bucketing_raises_rather_than_dropping_or_double_counting(db):
    rows = page.load_events(db[0])
    # Two rows sharing an id would make the buckets overlap; a row that matches
    # nothing is impossible by construction, so simulate the overlap directly.
    dup = dict(rows[0])
    dup["event_type"] = "conference"
    rows2 = rows + [dup]
    with pytest.raises(AssertionError, match="overlap"):
        page.bucket_events(rows2, (2025, 2026))
    # The chain cannot drop a row, so the coverage half is proven on the partition
    # checker directly: a bucket dict missing one id must raise.
    b = page.bucket_events(rows, (2025, 2026))
    b["listed"] = b["listed"][1:]
    with pytest.raises(AssertionError, match="do not cover"):
        page.assert_partition(b, rows)


# ------------------------------------------------------------------ page content

def test_page_shows_every_shown_row_by_id_and_counts_the_rest(db, cm):
    html, rows = _build(db, cm)
    shown_ids = {int(x) for x in re.findall(r'data-event-id="(\d+)"', html)}
    path, ids = db
    assert shown_ids == {ids["conf26"], ids["lead26"], ids["lead25"], ids["undated"], ids["dated_imprecise"]}
    ledger = dict(re.findall(r'<tr><td>([^<]+)</td><td class="n">(\d+)</td>', html))
    assert ledger["Total rows in the events database"] == str(len(rows))
    assert ledger["Shown: analyst days dated 2025-2026"] == "3"
    assert ledger["Shown: analyst days, date not yet known"] == "2"
    assert ledger["Counted only: retired (cancelled or superseded)"] == "1"
    assert "JPM 2027-02-22" in html
    assert re.search(r'<p class="headline"><b>5</b> analyst-day rows recorded.*<b>2</b> dated 2026.*<b>1</b> dated 2025', html)


def test_leads_render_as_leads_and_confirmed_as_confirmed(db, cm):
    html, _ = _build(db, cm)
    path, ids = db
    row = re.search(r'<tr class="([\w ]+)" data-event-id="%d"' % ids["lead26"], html).group(1)
    assert row.startswith("lead")
    row = re.search(r'<tr class="([\w ]+)" data-event-id="%d"' % ids["conf26"], html).group(1)
    assert row.startswith("confirmed")


def test_dated_but_imprecise_row_shows_its_hint_not_a_false_precise_date(db, cm):
    html, _ = _build(db, cm)
    path, ids = db
    tr = re.search(r'<tr[^>]*data-event-id="%d">(.*?)</tr>' % ids["dated_imprecise"], html).group(1)
    assert "Q3 2026" in tr and "Jul 1" not in tr


def test_null_source_url_renders_without_a_broken_anchor(db, cm):
    html, _ = _build(db, cm)
    path, ids = db
    tr = re.search(r'<tr[^>]*data-event-id="%d">(.*?)</tr>' % ids["undated"], html).group(1)
    assert "no link recorded" in tr and 'href=""' not in tr and 'href="None"' not in tr


def test_membership_tags_come_from_cm_and_scanned_set_from_load_core_watchlist(db, cm):
    html, _ = _build(db, cm)
    uni = page.load_universe(cm)
    assert uni["scanned"] == {"CI", "ADI", "FDX", "UPS", "WAY", "JPM", "ROIV", "LLY"}
    assert "NOSCAN" not in uni["scanned"]
    assert uni["tags"]["ROIV"] == {"Following"}
    assert "Following only" in html
    assert "<b>2</b> of your 8 Portfolio/Researching names" in html
    assert "Unscanned Portfolio/Researching: 2715.HK, NOSCAN." in html
    # the per-company table: a not-scanned name gets an EDGAR link, a no-CIK name a reason
    assert re.search(r'<b>NOSCAN</b>.*?not scanned.*?EDGAR full-text search', html)
    assert re.search(r'<b>2715\.HK</b>.*?no CIK in Coverage Manager', html)


def test_last_year_with_no_rows_reads_as_a_gap_not_an_absence(db, cm, tmp_path, monkeypatch):
    path = tmp_path / "e2.db"
    conn = init_db(path)
    _add(conn, monkeypatch, "CI", "investor_day", "2026-09-30")
    conn.close()
    html, _ = _build((path, {}), cm)
    assert "2025: none recorded. Not searched, so this is a gap, not an absence." in html
    assert "<b>0</b> dated 2025" in html
    assert 'data-banner="reset"' not in html


# ------------------------------------------------------------------ guards

@pytest.mark.parametrize("offset_days,fires", [(0, False), (1, False), (2, True)])
def test_reset_banner_boundary_on_the_production_timestamp_shape(tmp_path, monkeypatch, cm, offset_days, fires):
    first = datetime(FLOOR.year, FLOOR.month, FLOOR.day, 15, 54, 22, tzinfo=timezone.utc)
    first = first.replace(day=FLOOR.day) + (date.fromordinal(FLOOR.toordinal() + offset_days) - FLOOR)
    path = tmp_path / "e.db"
    conn = init_db(path)
    _add(conn, monkeypatch, "CI", "investor_day", "2026-09-30", seen=first.isoformat(timespec="seconds"))
    conn.close()
    rows = page.load_events(path)
    assert rows[0]["first_seen_at"].endswith("+00:00")       # the shape events_repo writes
    html = page.build(rows, page.load_universe(cm), _prov(), TODAY)
    assert ("reset" in _banners(html)) is fires


def test_reset_banner_on_an_empty_database(tmp_path, cm):
    path = tmp_path / "empty.db"
    init_db(path).close()
    html = page.build(page.load_events(path), page.load_universe(cm), _prov(), TODAY)
    assert "reset" in _banners(html)
    assert "empty database" in html


def test_stale_banner_fires_at_nine_days_not_eight(db, cm):
    html8, _ = _build(db, cm, prov=_prov(created="2026-09-26T12:00:00Z"))
    html9, _ = _build(db, cm, prov=_prov(created="2026-09-25T12:00:00Z"))
    assert "stale" not in _banners(html8)
    assert "stale" in _banners(html9)


def test_staleness_is_measured_from_the_last_scheduled_run_not_a_newer_manual_one(db, cm):
    """codex r1: a successful dry-run dispatch re-uploads the old DB under a new date."""
    html, _ = _build(db, cm, prov=_prov(created="2026-10-03T12:00:00Z",
                                        scheduled="2026-09-14T12:00:00Z"))
    assert "stale" in _banners(html)
    html, _ = _build(db, cm, prov=_prov(created="2026-10-03T12:00:00Z", scheduled=None))
    assert "no-scheduled" in _banners(html)


def _run_page_script(html: str, today_iso: str, tmp_path) -> dict:
    """Execute the page's own inline script under node with a minimal DOM double and a
    frozen clock; return the stale-live banner's resulting state."""
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    live = re.search(r'<div class="banner amber" data-banner="stale-live"([^>]*)>', html)
    built = 'data-banner="stale"' in html
    attrs = dict(re.findall(r'(data-[\w-]+)="([^"]*)"', live.group(1))) if live else {}
    harness = f"""
const RealDate = Date;
const FIXED = RealDate.parse({json.dumps(today_iso + "T12:00:00Z")});
global.Date = class extends RealDate {{ constructor(...a) {{ super(...(a.length ? a : [FIXED])); }}
  static UTC(...a) {{ return RealDate.UTC(...a); }} }};
const age = {{ textContent: "" }};
const live = {json.dumps(bool(live))} ? {{ hidden: true, attrs: {json.dumps(attrs)},
  getAttribute(k) {{ return this.attrs[k]; }}, querySelector() {{ return age; }} }} : null;
const built = {json.dumps(built)} ? {{ hidden: false }} : null;
global.document = {{ querySelector(sel) {{
  if (sel.includes("stale-live")) return live;
  if (sel.includes('"stale"')) return built;
  return null; }} }};
{script}
console.log(JSON.stringify({{ liveHidden: live ? live.hidden : null, age: age.textContent,
  builtHidden: built ? built.hidden : null }}));
"""
    f = tmp_path / "harness.js"
    f.write_text(harness, encoding="utf-8")
    out = subprocess.run(["node", str(f)], cwd=str(tmp_path), capture_output=True,
                         text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_published_page_turns_stale_as_it_ages_without_a_rebuild(db, cm, tmp_path):
    """codex r1: the build-time check froze at build time. Built fresh on 2026-10-04 from a
    2026-09-28 run, the page must show nothing that day and the stale banner on 10-20."""
    html, _ = _build(db, cm)
    assert "stale" not in _banners(html)
    fresh = _run_page_script(html, "2026-10-04", tmp_path)
    assert fresh["liveHidden"] is True
    edge = _run_page_script(html, "2026-10-06", tmp_path)       # 8 days: not yet
    assert edge["liveHidden"] is True
    later = _run_page_script(html, "2026-10-20", tmp_path)
    assert later["liveHidden"] is False and later["age"] == "22"


def test_newer_failed_run_is_announced(db, cm):
    skipped = [{"artifact_created_at": "2026-10-05T12:00:00Z", "run_id": 9,
                "run_conclusion": "failure", "run_url": "https://github.com/x/y/actions/runs/9"}]
    html, _ = _build(db, cm, prov=_prov(skipped=skipped))
    assert "newer-failed" in _banners(html)
    assert "showing the last successful database from 2026-09-28" in html
    clean, _ = _build(db, cm)
    assert "newer-failed" not in _banners(clean)


def test_missing_provenance_is_a_red_banner_not_silence(db, cm):
    html, _ = _build(db, cm, prov=None)
    assert "unverified" in _banners(html)


# ------------------------------------------------------------------ artifact selection

def test_select_artifact_skips_newer_failed_runs_and_expired_and_non_master():
    arts = [
        {"id": 4, "name": "analyst-days-db", "expired": False, "created_at": "2026-10-05T12:00:00Z",
         "workflow_run": {"id": 40, "head_branch": "master"}},
        {"id": 3, "name": "analyst-days-db", "expired": False, "created_at": "2026-10-04T12:00:00Z",
         "workflow_run": {"id": 30, "head_branch": "feature"}},
        {"id": 2, "name": "analyst-days-db", "expired": False, "created_at": "2026-09-28T12:00:00Z",
         "workflow_run": {"id": 20, "head_branch": "master"}},
        {"id": 1, "name": "analyst-days-db", "expired": True, "created_at": "2026-09-29T12:00:00Z",
         "workflow_run": {"id": 10, "head_branch": "master"}},
        {"id": 0, "name": "other", "expired": False, "created_at": "2026-10-06T12:00:00Z",
         "workflow_run": {"id": 1, "head_branch": "master"}},
    ]
    concl = {40: "failure", 20: "success"}

    def info(rid):
        return {"conclusion": concl.get(rid, "success"), "event": "schedule"}
    chosen, skipped, anchor = page.select_artifact(arts, info)
    assert chosen["id"] == 2
    assert [s["run_id"] for s in skipped] == [40]
    assert anchor == "2026-09-28T12:00:00Z"
    # a feature-branch or expired artifact must never be consulted at all
    asked = []
    page.select_artifact(arts, lambda rid: (asked.append(rid), info(rid))[1])
    assert 30 not in asked and 10 not in asked and 1 not in asked


def test_a_successful_manual_run_is_selected_but_does_not_reset_the_freshness_clock():
    """Production restores the newest successful artifact, dispatch or not, so the page
    renders that DB; but a dry-run dispatch re-uploads unchanged data, so freshness is
    anchored on the newest successful scheduled run behind it."""
    arts = [
        {"id": 5, "name": "analyst-days-db", "expired": False, "created_at": "2026-10-03T12:00:00Z",
         "workflow_run": {"id": 50, "head_branch": "master"}},
        {"id": 2, "name": "analyst-days-db", "expired": False, "created_at": "2026-09-14T12:00:00Z",
         "workflow_run": {"id": 20, "head_branch": "master"}},
    ]
    events = {50: "workflow_dispatch", 20: "schedule"}
    chosen, skipped, anchor = page.select_artifact(
        arts, lambda rid: {"conclusion": "success", "event": events[rid]})
    assert chosen["id"] == 5 and skipped == []
    assert anchor == "2026-09-14T12:00:00Z"


def test_select_artifact_returns_none_when_every_run_failed():
    arts = [{"id": 4, "name": "analyst-days-db", "expired": False, "created_at": "2026-10-05T12:00:00Z",
             "workflow_run": {"id": 40, "head_branch": "master"}}]
    chosen, skipped, anchor = page.select_artifact(
        arts, lambda rid: {"conclusion": "failure", "event": "schedule"})
    assert chosen is None and len(skipped) == 1 and anchor is None


# ------------------------------------------------------------------ CLI posture

def test_cli_refuses_to_fall_back_to_the_dev_sandbox_db(tmp_path, monkeypatch, cm, capsys):
    monkeypatch.setattr(page, "DEFAULT_DB", tmp_path / "nope" / "events.db")
    monkeypatch.setattr(page, "SNAPSHOT_DIR", tmp_path / "nope")
    rc = page.main(["--out", str(tmp_path / "o.html"), "--cm", str(cm), "--today", "2026-10-04"])
    assert rc == 2
    assert "never reads data/events.db" in capsys.readouterr().err
    assert not (tmp_path / "o.html").exists()


def test_cli_builds_from_an_explicit_db_and_reads_sibling_provenance(db, cm, tmp_path):
    (db[0].parent / page.PROVENANCE_NAME).write_text(json.dumps(_prov()), encoding="utf-8")
    out = tmp_path / "o.html"
    rc = page.main(["--db", str(db[0]), "--out", str(out), "--cm", str(cm), "--today", "2026-10-04"])
    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert "unverified" not in _banners(html) and "run 7" in html


def test_generated_page_and_snapshot_are_gitignored(tmp_path):
    """The repo is public; the page is published as a private Artifact, never committed.
    Run from tmp_path with `git -C` -- conftest refuses children started in the repo."""
    def ignored(rel):
        return subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", rel],
                              cwd=str(tmp_path), capture_output=True).returncode
    assert ignored("exports/analyst_day_listing.html") == 0
    assert ignored("data/ci_snapshot/events.db") == 0
    assert ignored("data/ci_snapshot/provenance.json") == 0
    assert ignored("scripts/build_analyst_day_page.py") == 1   # the negative: git is real


def test_edgar_link_pads_cik_and_scopes_the_date_range():
    url = page.edgar_fts_url("320193", date(2025, 1, 1), date(2026, 10, 4))
    assert "ciks=0000320193" in url and "startdt=2025-01-01" in url and "enddt=2026-10-04" in url


@pytest.mark.parametrize("url", ["javascript:alert(document.domain)", " JavaScript:alert(1)",
                                 "data:text/html,<script>alert(1)</script>", "vbscript:x"])
def test_active_scheme_source_urls_never_become_links(url):
    """codex r2: source_url is classifier output over web results -- untrusted."""
    html = page._sources_html({"sources": [{"source_type": "TAVILY_HIT", "source_url": url,
                                            "source_excerpt": "x"}]})
    assert "href" not in html and "non-web link withheld" in html
    ok = page._sources_html({"sources": [{"source_type": "IR_PAGE", "source_url": "https://ir.x.com/e",
                                          "source_excerpt": "x"}]})
    assert 'href="https://ir.x.com/e"' in ok


def test_a_deliberate_bootstrap_is_announced_amber_not_red(tmp_path, monkeypatch, cm):
    """#456: a Monday run dispatched with bootstrap_db=true stamps
    schema_meta.bootstrapped_at. Without it the first rows after a bootstrap trip
    the red "history is missing" banner forever, indistinguishable from the loss
    it exists to catch."""
    from src import state_guard
    path = tmp_path / "boot.db"
    conn = init_db(path)
    state_guard.record_bootstrap(conn, "2026-11-02T12:15:00+00:00")
    _add(conn, monkeypatch, "CI", "investor_day", "2026-12-10", seen="2026-11-02T12:40:00+00:00")
    conn.close()
    rows = page.load_events(path)
    uni = page.load_universe(cm)
    with_stamp = page.build(rows, uni, _prov(), TODAY, bootstrapped_at=page.load_bootstrapped_at(path))
    assert "bootstrapped" in _banners(with_stamp) and "reset" not in _banners(with_stamp)
    assert "restarted on 2026-11-02" in with_stamp
    without = page.build(rows, uni, _prov(), TODAY)                 # same rows, no stamp -> red
    assert "reset" in _banners(without)
    # Rows OLDER than the stamp are not explained by it: still red.
    conn = init_db(path)
    _add(conn, monkeypatch, "MSFT", "investor_day", "2026-12-11", seen="2026-09-01T12:00:00+00:00")
    conn.close()
    older = page.build(page.load_events(path), uni, _prov(), TODAY, bootstrapped_at=page.load_bootstrapped_at(path))
    assert "reset" in _banners(older)
