"""Conference calendar — schema v3, seeder, and the #conferences digest.

The table had no reader and no test until now: seeded 2026-08-05 and touched by
nothing but the seeder since. These pin the three things that actually broke or
could break here — the duplicate-DDL trap, the dateless policy, and rollover.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

from src.outputs import slack as slack_out
from src.seed_conferences import CONFERENCES, seed, unconfirmed
from src.state.schema import init_db, schema_version


@pytest.fixture()
def db(tmp_path):
    conn = init_db(tmp_path / "events.db")
    yield conn
    conn.close()


def test_schema_v3_adds_conference_columns(db):
    assert schema_version(db) >= 3
    cols = {r[1] for r in db.execute("PRAGMA table_info(conferences)")}
    assert {"sector", "series", "host_ticker"} <= cols


def test_migration_is_additive_on_an_existing_v2_db(tmp_path):
    """v3 must ALTER an existing table, not depend on a fresh CREATE.

    The last incident here was a second CREATE TABLE IF NOT EXISTS silently
    no-opping against a live table, so the new definition never applied.
    """
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES ('schema_version', '2');
        CREATE TABLE conferences (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE, short_name TEXT,
          start_date TEXT NOT NULL, end_date TEXT NOT NULL,
          location TEXT, url TEXT, notes TEXT);
        INSERT INTO conferences (name, short_name, start_date, end_date)
          VALUES ('Legacy Meeting', 'LEG 2026', '2026-09-01', '2026-09-02');
        """
    )
    raw.commit()
    raw.close()

    conn = init_db(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conferences)")}
    assert {"sector", "series", "host_ticker"} <= cols
    # The pre-existing row survives untouched.
    row = conn.execute("SELECT name, start_date, sector FROM conferences").fetchone()
    assert row["name"] == "Legacy Meeting"
    assert row["start_date"] == "2026-09-01"
    assert row["sector"] is None
    conn.close()


def test_seed_writes_dated_rows_and_refuses_undated(tmp_path):
    path = tmp_path / "events.db"
    added, updated = seed(str(path))
    dated = [c for c in CONFERENCES if c.start and c.end]
    assert added == len(dated)
    assert updated == 0

    conn = init_db(path)
    n = conn.execute("SELECT COUNT(*) FROM conferences").fetchone()[0]
    assert n == len(dated)
    # Every undated circuit entry stayed out — the whole point of the policy.
    stored = {r[0] for r in conn.execute("SELECT short_name FROM conferences")}
    for c in unconfirmed():
        assert c.short_name not in stored
    conn.close()


def test_seed_is_idempotent(tmp_path):
    """Re-running must update in place, never duplicate — the weekly workflow
    re-seeds on every fire to survive artifact expiry."""
    path = tmp_path / "events.db"
    added_1, _ = seed(str(path))
    added_2, updated_2 = seed(str(path))
    assert added_2 == 0
    assert updated_2 == added_1

    conn = init_db(path)
    assert conn.execute("SELECT COUNT(*) FROM conferences").fetchone()[0] == added_1
    conn.close()


def test_seed_populates_sector_and_series(tmp_path):
    path = tmp_path / "events.db"
    seed(str(path))
    conn = init_db(path)
    rows = conn.execute("SELECT sector, series, host_ticker FROM conferences").fetchall()
    assert rows
    assert all(r["sector"] == "Healthcare" for r in rows)
    assert all(r["series"] for r in rows)
    # No healthcare conference is company-owned — ASCO owns ASCO.
    assert all(r["host_ticker"] is None for r in rows)
    conn.close()


def test_series_repeats_across_years_but_instances_are_unique():
    """`series` is deliberately NOT unique — that is what makes it a series.

    Uniqueness belongs to the instance: `short_name` is the seeder's upsert key
    and `name` is UNIQUE in the table, so both must be distinct per instance or
    two different years silently overwrite each other.
    """
    shorts = [c.short_name for c in CONFERENCES]
    names = [c.name for c in CONFERENCES]
    assert len(shorts) == len(set(shorts)), "short_name is the upsert key"
    assert len(names) == len(set(names)), "name is UNIQUE in the table"
    # At least one series genuinely recurs, else `series` is carrying nothing.
    from collections import Counter
    assert max(Counter(c.series for c in CONFERENCES).values()) > 1


def test_held_instances_are_present_for_last_year_and_this_year():
    """The calendar must carry history, not just the forward view.

    The seeder was forward-only until 2026-08-12, so 2025 and H1-2026 were
    entirely absent — conferences that had already happened were recorded
    nowhere.
    """
    years = {c.start[:4] for c in CONFERENCES if c.start}
    assert {"2025", "2026", "2027"} <= years


def test_digest_groups_by_sector_and_flags_undated(db):
    db.execute(
        "INSERT INTO conferences (name, short_name, series, sector, start_date, "
        "end_date, location) VALUES "
        "('Big Onc Meeting','ONC 2027','onc','Healthcare','2027-06-01','2027-06-04','Chicago, IL')")
    db.execute(
        "INSERT INTO conferences (name, short_name, series, sector, host_ticker, "
        "start_date, end_date, location) VALUES "
        "('Dev Keynote','DEV 2027','dev','Technology','AAPL','2027-06-07','2027-06-11','Cupertino, CA')")
    db.commit()

    blocks, count = slack_out.build_conferences_blocks(db, "2026-08-12")
    assert count == 2
    text = "\n".join(
        b.get("text", {}).get("text", "") for b in blocks if b.get("type") == "section"
    )
    assert "*Healthcare (1)*" in text
    assert "*Technology (1)*" in text
    # Healthcare leads regardless of insertion or date order.
    assert text.index("*Healthcare (1)*") < text.index("*Technology (1)*")
    assert "AAPL" in text            # host_ticker rendered


def test_undated_backlog_renders_when_there_IS_one(db, monkeypatch):
    """The dateless mechanism, tested independently of live seed data.

    Every circuit entry has a confirmed date as of 2026-08-12, so the section is
    correctly absent today — but the policy is what tech and consumer meetings
    will land under, and it must not rot while the backlog happens to be empty.
    """
    import src.seed_conferences as seed_mod

    fake = seed_mod.Conference(
        name="Some Meeting", short_name="SOME", series="some",
        start=None, end=None, location=None, url="https://example.com",
        date_status="unconfirmed", verified_on=None,
        why_it_matters="On the circuit. CONFIRM DATE.")
    monkeypatch.setattr(seed_mod, "unconfirmed", lambda: [fake])

    blocks, _ = slack_out.build_conferences_blocks(db, "2026-08-12")
    text = "\n".join(
        b.get("text", {}).get("text", "") for b in blocks if b.get("type") == "section"
    )
    assert "Dates needed (1)" in text
    assert "SOME" in text


def test_no_undated_backlog_means_no_section(db):
    """All 39 instances carry a verified date today, so nothing should claim a
    backlog that does not exist."""
    from src.seed_conferences import unconfirmed
    assert unconfirmed() == []
    blocks, _ = slack_out.build_conferences_blocks(db, "2026-08-12")
    text = "\n".join(
        b.get("text", {}).get("text", "") for b in blocks if b.get("type") == "section"
    )
    assert "Dates needed" not in text


def test_digest_flags_rollover_when_a_series_has_only_past_instances(db):
    db.execute(
        "INSERT INTO conferences (name, short_name, series, sector, start_date, "
        "end_date) VALUES "
        "('Stale Meeting 2025','STALE 2025','stale','Healthcare','2025-03-01','2025-03-03')")
    db.execute(
        "INSERT INTO conferences (name, short_name, series, sector, start_date, "
        "end_date) VALUES "
        "('Live Meeting 2025','LIVE 2025','live','Healthcare','2025-05-01','2025-05-03')")
    db.execute(
        "INSERT INTO conferences (name, short_name, series, sector, start_date, "
        "end_date) VALUES "
        "('Live Meeting 2027','LIVE 2027','live','Healthcare','2027-05-01','2027-05-03')")
    db.commit()

    _upcoming, rollover = slack_out.query_conferences(db, "2026-08-12")
    shorts = {r["short_name"] for r in rollover}
    # 'stale' has no future instance -> needs rollover. 'live' does -> does not,
    # and its own past instance must not be listed either.
    assert shorts == {"STALE 2025"}


def test_digest_survives_null_sector(db):
    db.execute(
        "INSERT INTO conferences (name, short_name, start_date, end_date) VALUES "
        "('Unslugged Meeting','UNS 2027','2027-02-01','2027-02-02')")
    db.commit()
    blocks, count = slack_out.build_conferences_blocks(db, "2026-08-12")
    assert count == 1
    text = "\n".join(
        b.get("text", {}).get("text", "") for b in blocks if b.get("type") == "section"
    )
    assert "Unclassified (1)" in text


def test_unset_webhook_seeds_anyway_and_degrades_to_a_warning(tmp_path, monkeypatch):
    """The webhook-missing branch, which no test reached until it crashed live.

    The seed must still run — it is the half that cannot be skipped, and it is
    the only path that rebuilds this table. The missing post is recorded as a
    heartbeat warning (=> `partial`) rather than raising, so one unprovisioned
    secret does not red-line the whole weekly cron.
    """
    import argparse

    from src import cli as cli_mod

    monkeypatch.delenv("SLACK_WEBHOOK_CONFERENCES", raising=False)
    args = argparse.Namespace(db=str(tmp_path / "events.db"), dry_run=False)

    assert cli_mod.cmd_conferences_digest(args) == 0
    conn = init_db(tmp_path / "events.db")
    assert conn.execute("SELECT COUNT(*) FROM conferences").fetchone()[0] > 0
    conn.close()
    # ...and the skip is carried where a reader will see it.
    assert "SLACK_WEBHOOK_CONFERENCES" in args.health_conferences_warning


def test_a_skipped_conference_post_makes_the_weekly_heartbeat_partial(monkeypatch):
    """The warning must reach #status-reports, or the skip is silent."""
    import argparse

    from src import cli as cli_mod

    posted: list = []
    monkeypatch.setattr(cli_mod.health_mod, "post_health", posted.append)

    args = argparse.Namespace(
        dry_run=False, db="unused.db", no_slack=True,
        health_discover={}, health_remind={}, health_digest={},
        health_phase_errors=[], health_phase_rcs={},
        health_conferences_warning="SLACK_WEBHOOK_CONFERENCES not set - skipped",
    )
    cli_mod._post_weekly_health(args, __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc))

    assert posted, "no heartbeat posted"
    hb = posted[0]
    assert hb.status == "partial"
    assert any("SLACK_WEBHOOK_CONFERENCES" in w for w in hb.warnings)


def test_published_page_filters_are_wired_to_the_data(tmp_path):
    """Every filter chip must match real items, and every item must be reachable.

    The failure mode here is silent and total: a chip whose `data-value` does not
    equal the slug on the items filters to zero rows, and an item whose slug no
    chip carries can never be shown. Both render as a page that looks fine and
    hides data. Checked against the generated HTML, so a change to either side of
    the slug contract fails here.

    (The click behaviour itself was verified by running the page's own script
    under jsdom; that needs a Node toolchain this repo does not carry, so what is
    pinned permanently is the contract the script depends on.)
    """
    import re
    import subprocess

    from src.seed_conferences import seed as seed_conferences

    db_path = tmp_path / "events.db"
    seed_conferences(str(db_path))
    out = tmp_path / "page.html"
    rc = subprocess.call(
        [sys.executable, str(REPO / "scripts" / "build_conference_page.py"),
         "--db", str(db_path), "--out", str(out),
         "--years", "2025", "2026", "--today", "2026-08-12"],
        cwd=str(REPO),
    )
    assert rc == 0
    html = out.read_text(encoding="utf-8")

    items = re.findall(r'data-sector="([^"]+)" data-kind="([^"]+)"', html)
    chips = re.findall(r'data-group="(\w+)" data-value="([^"]+)"', html)
    assert items, "no conference items rendered"

    for axis, idx in (("sector", 0), ("kind", 1)):
        chip_vals = {v for g, v in chips if g == axis} - {"all"}
        item_vals = {i[idx] for i in items}
        assert not (item_vals - chip_vals), (
            f"{axis}: items unreachable by any chip: {item_vals - chip_vals}")
        assert not (chip_vals - item_vals), (
            f"{axis}: chips matching nothing: {chip_vals - item_vals}")

    # Elements the filter script addresses by name — a rename silently no-ops it.
    assert 'id="shown"' in html
    for block in re.findall(r'<article class="q[^"]*">.*?</article>', html, re.S):
        assert "data-count" in block and '<p class="empty"' in block
    for block in re.findall(r'<section class="year">.*?</section>', html, re.S):
        assert "data-year-count" in block


def test_conferences_digest_posts_to_its_own_webhook(db, monkeypatch):
    """#conferences, never #analyst-days — the channel split is the point."""
    seen = {}

    def fake_post(payload, env_var=slack_out.WEBHOOK_ENV):
        seen["env"] = env_var

    monkeypatch.setattr(slack_out, "_post", fake_post)
    slack_out.post_conferences_digest(db, "2026-08-12")
    assert seen["env"] == "SLACK_WEBHOOK_CONFERENCES"
