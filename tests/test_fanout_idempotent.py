"""Board #456: fan-out must be idempotent across a DB REBUILD, and its failures
must reach the heartbeat.

Before 2026-10-04 the only dedupe was the events.{calendar_event_id,
ticktick_task_id, slack_posted_at} columns -- which live in the rebuilt DB and
are therefore NULL after a rebuild. `gcal.find_existing_by_event_key` existed for
exactly this and had zero callers, so a rebuilt DB doubled Calendar, TickTick
AND Slack (reproduced against 84c39b6 with the fakes below: 2 / 2 / 2).

Everything here runs against fakes. No network, no Slack, no Calendar, no TickTick.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src import cli as cli_mod
from src.outputs import gcal as gcal_out
from src.outputs import slack as slack_out
from src.outputs import ticktick as ticktick_out
from src.state.events_repo import CandidateEvent, CandidateSource, upsert_event
from src.state.schema import init_db

START = (date.today() + timedelta(days=40)).isoformat()


# ------------------------------------------------------------------ fakes

class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeCalendar:
    """Just enough of the googleapiclient surface: insert/update/list with
    privateExtendedProperty filtering and showDeleted semantics."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.n = 0
        self.list_calls = 0
        self.list_error: Exception | None = None

    def events(self):
        return self

    def insert(self, calendarId, body):
        self.n += 1
        eid = f"g{self.n}"
        self.items[eid] = {**body, "id": eid, "status": "confirmed"}
        return _Exec({"id": eid})

    def update(self, calendarId, eventId, body):
        if eventId not in self.items:
            return _Exec(_HttpError(404))
        self.items[eventId].update(body)
        return _Exec({"id": eventId})

    def list(self, calendarId, privateExtendedProperty, maxResults, singleEvents, showDeleted=False):
        self.list_calls += 1
        if self.list_error:
            return _Exec(self.list_error)
        k, v = privateExtendedProperty.split("=", 1)
        hits = [it for it in self.items.values()
                if it["extendedProperties"]["private"].get(k) == v
                and (showDeleted or it["status"] != "cancelled")]
        return _Exec({"items": [{"id": it["id"], "status": it["status"]} for it in hits]})

    def cancel(self, eid):
        self.items[eid]["status"] = "cancelled"


class _Resp:
    def __init__(self, status):
        self.status = status


class _HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = _Resp(status)


class _HttpResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else str(payload)[:200]

    def json(self):
        return self._payload


class FakeTickTick:
    """HTTP-level fake of the TickTick Open API, patched in at `requests` so the
    module's real request code runs. Encodes the production quirk Codex r1 caught:
    the documented single-task GET `/task/{projectId}/{taskId}` returns 404 for
    VALID ids (earnings_agent verified 8/8, 2026-07-22). A fake that replaced
    `update_task` wholesale could not see that, and certified a broken adoption."""

    API = "https://api.ticktick.com/open/v1"

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.n = 0
        self.list_calls = 0
        self.single_gets = 0
        self.fail_posts_with: int | None = None

    def install(self, monkeypatch):
        monkeypatch.setattr(ticktick_out.requests, "get", self.get)
        monkeypatch.setattr(ticktick_out.requests, "post", self.post)

    def get(self, url, headers=None, timeout=None):
        path = url[len(self.API):]
        if path == "/project":
            return _HttpResp(200, [{"id": "L1", "name": ticktick_out.LIST_NAME}])
        if path == "/project/L1/data":
            self.list_calls += 1
            return _HttpResp(200, {"project": {"id": "L1"}, "columns": [],
                                   "tasks": [dict(t) for t in self.tasks.values()
                                             if t.get("status", 0) == 0]})
        if path.startswith("/task/"):
            self.single_gets += 1
            return _HttpResp(404, {"errorCode": "task_not_found"})  # the real quirk
        return _HttpResp(404)

    def post(self, url, headers=None, json=None, timeout=None):
        path = url[len(self.API):]
        if self.fail_posts_with:
            return _HttpResp(self.fail_posts_with, {"error": "boom"})
        if path == "/task":
            self.n += 1
            tid = f"t{self.n}"
            self.tasks[tid] = {**json, "id": tid, "status": 0}
            return _HttpResp(200, self.tasks[tid])
        if path.startswith("/task/"):
            tid = path.split("/")[2]
            if tid not in self.tasks:
                return _HttpResp(404)
            self.tasks[tid].update(json)
            return _HttpResp(200, self.tasks[tid])
        return _HttpResp(404)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "fake-calendar")
    monkeypatch.setenv("TICKTICK_ACCESS_TOKEN", "fake")
    cal = FakeCalendar()
    tt = FakeTickTick()
    tt.install(monkeypatch)
    slack: list[str] = []
    monkeypatch.setattr(gcal_out, "get_service", lambda: cal)
    monkeypatch.setattr(slack_out, "post_confirmed", lambda row: slack.append(row["ticker"]))
    return argparse.Namespace(cal=cal, tt=tt, slack=slack)


def _args(**over):
    base = dict(no_slack=False, no_gcal=False, no_ticktick=False)
    base.update(over)
    return argparse.Namespace(**base)


def _event(conn, ticker="CI", start=START, etype="investor_day"):
    upsert_event(conn, CandidateEvent(
        ticker=ticker, company_name=f"{ticker} Inc", event_type=etype, start_date=start,
        confidence=0.95, date_grounded=True,
        sources=[CandidateSource(source_type="8K", source_url="https://sec.gov/x")]))


def _row(conn, ticker="CI"):
    return conn.execute("SELECT * FROM events WHERE ticker = ?", (ticker,)).fetchone()


# ------------------------------------------------------------------ the rebuild

def test_a_rebuilt_db_adopts_instead_of_duplicating(tmp_path, world):
    c1 = init_db(tmp_path / "run1.db"); _event(c1)
    r1 = cli_mod._fan_out_confirmed(c1, _args())
    assert (r1["fanout_gcal"], r1["fanout_ticktick"], r1["fanout_slack"]) == (1, 1, 1)
    assert r1["fanout_errors"] == 0

    # Artifact lost; the same event rediscovered into a fresh DB with NULL ids.
    c2 = init_db(tmp_path / "run2.db"); _event(c2)
    assert _row(c2)["calendar_event_id"] is None
    r2 = cli_mod._fan_out_confirmed(c2, _args())

    assert len(world.cal.items) == 1, "calendar duplicated"
    assert len(world.tt.tasks) == 1, "ticktick duplicated"
    assert world.slack == ["CI"], "slack re-pinged"
    assert r2["fanout_adopted"] == 1 and r2["fanout_slack"] == 0
    row = _row(c2)
    assert row["calendar_event_id"] == "g1" and row["ticktick_task_id"] == "t1"
    assert row["slack_posted_at"] is not None  # suppressed, and will not retry

    # Third run: nothing left to do, no API writes.
    r3 = cli_mod._fan_out_confirmed(c2, _args())
    assert (r3["fanout_gcal"], r3["fanout_ticktick"], r3["fanout_slack"], r3["fanout_adopted"]) == (0, 0, 0, 0)


def test_legacy_ticktick_task_without_marker_is_adopted_by_title_and_due_day(tmp_path, world):
    # A task created before the marker existed: same title, due the day before in UTC
    # (TickTick all-day dates in the list's zone), no key line in content.
    before = (date.fromisoformat(START) - timedelta(days=1)).isoformat()
    world.tt.tasks["legacy"] = {"id": "legacy", "title": "[Investor Day] CI",
                                "content": "CI Inc\nConfidence: 0.95",
                                "dueDate": f"{before}T04:00:00.000+0000", "status": 0}
    conn = init_db(tmp_path / "e.db"); _event(conn)
    cli_mod._fan_out_confirmed(conn, _args())
    assert len(world.tt.tasks) == 1
    assert _row(conn)["ticktick_task_id"] == "legacy"
    assert ticktick_out.KEY_MARKER in world.tt.tasks["legacy"]["content"]  # marker written on adopt


def test_legacy_match_never_adopts_a_sibling_rows_task(tmp_path, world):
    """Two rows one day apart (a date correction left both): the second must not
    grab the first row's task, or --retire on one deletes the other's."""
    conn = init_db(tmp_path / "e.db")
    _event(conn, start=START)
    cli_mod._fan_out_confirmed(conn, _args())
    first_task = _row(conn)["ticktick_task_id"]
    # strip the marker to make it look legacy
    world.tt.tasks[first_task]["content"] = "CI Inc"
    nxt = (date.fromisoformat(START) + timedelta(days=1)).isoformat()
    _event(conn, start=nxt)
    cli_mod._fan_out_confirmed(conn, _args())
    ids = [r[0] for r in conn.execute("SELECT ticktick_task_id FROM events ORDER BY start_date")]
    assert ids[0] == first_task and ids[1] not in (None, first_task)
    assert len(world.tt.tasks) == 2


def test_open_tasks_are_fetched_once_per_run(tmp_path, world):
    conn = init_db(tmp_path / "e.db")
    for t in ("AAA", "BBB", "CCC"):
        _event(conn, ticker=t)
    cli_mod._fan_out_confirmed(conn, _args())
    assert world.tt.list_calls == 1


# ------------------------------------------------------------------ retired events

def test_cancelled_calendar_match_pushes_nothing_and_names_a_retire_candidate(tmp_path, world):
    c1 = init_db(tmp_path / "run1.db"); _event(c1)
    cli_mod._fan_out_confirmed(c1, _args())
    world.cal.cancel("g1")           # --retire / deleted by hand
    del world.tt.tasks["t1"]         # --retire deletes the task too

    c2 = init_db(tmp_path / "run2.db"); _event(c2)
    r = cli_mod._fan_out_confirmed(c2, _args())
    assert len(world.cal.items) == 1 and world.cal.items["g1"]["status"] == "cancelled"
    assert world.tt.tasks == {}, "TickTick must be gated on the same evidence"
    assert world.slack == ["CI"], "Slack must be gated on the same evidence"
    row = _row(c2)
    assert row["calendar_event_id"] is None, "a cancelled id must not be stored"
    assert row["slack_posted_at"] is not None
    assert r["fanout_retired_matches"] == [f"CI investor_day {START}"]
    assert r["fanout_errors"] == 0
    # Quarantined as a ROW (Codex r2): terminal, so the remind phase that runs
    # next in --weekly, the digests and the export all skip it.
    assert row["status"] == "cancelled"
    assert "UPDATE events SET status='confirmed'" in row["notes"]
    from src.reminders import due_reminders
    assert due_reminders(c2, (date.fromisoformat(START) - timedelta(days=7)).isoformat()) == []
    from src.state.events_repo import recompute_statuses
    recompute_statuses(c2)
    assert _row(c2)["status"] == "cancelled"
    # Rediscovered again next week: merges into the retired row, stays retired.
    _event(c2)
    assert _row(c2)["status"] == "cancelled"
    assert cli_mod._fan_out_confirmed(c2, _args())["fanout_retired_matches"] == []


def test_live_match_is_preferred_over_a_cancelled_one(tmp_path, world):
    c1 = init_db(tmp_path / "run1.db"); _event(c1)
    cli_mod._fan_out_confirmed(c1, _args())
    world.cal.cancel("g1")
    # re-created by hand later, same key
    row = _row(c1)
    world.cal.insert("x", gcal_out._build_event_body(row, None, None))
    c2 = init_db(tmp_path / "run2.db"); _event(c2)
    cli_mod._fan_out_confirmed(c2, _args())
    assert _row(c2)["calendar_event_id"] == "g2"
    assert len(world.cal.items) == 2


# ------------------------------------------------------------------ never blind-insert

def test_lookup_error_blocks_the_insert_and_is_counted(tmp_path, world):
    world.cal.list_error = RuntimeError("503 backend")
    conn = init_db(tmp_path / "e.db"); _event(conn)
    r = cli_mod._fan_out_confirmed(conn, _args())
    assert world.cal.items == {}, "an insert we cannot prove unique must not happen"
    assert r["fanout_errors"] >= 1
    assert any("Calendar CI" in f and "RuntimeError" in f for f in r["fanout_failures"])
    assert _row(conn)["calendar_event_id"] is None  # retried next run


def test_stored_id_update_failure_other_than_404_propagates(tmp_path, world):
    conn = init_db(tmp_path / "e.db"); _event(conn)
    cli_mod._fan_out_confirmed(conn, _args())
    row = _row(conn)

    real_update = world.cal.update
    calls = []

    def flaky(calendarId, eventId, body):
        # 500 on the STORED-id update only; a later key-lookup adoption would succeed,
        # so falling through on a non-404 is observable as "no raise".
        calls.append(eventId)
        if len(calls) == 1:
            return _Exec(_HttpError(500))
        return real_update(calendarId, eventId, body)
    world.cal.update = flaky
    with pytest.raises(_HttpError):
        gcal_out.upsert_calendar_event(world.cal, conn, row)
    assert calls == ["g1"], "a transient error must not fall through to lookup/insert"
    assert len(world.cal.items) == 1


def test_stored_id_gone_recreates_via_lookup(tmp_path, world):
    conn = init_db(tmp_path / "e.db"); _event(conn)
    cli_mod._fan_out_confirmed(conn, _args())
    del world.cal.items["g1"]
    res = gcal_out.upsert_calendar_event(world.cal, conn, _row(conn))
    assert res.action == "created" and res.gcal_id == "g2"
    assert world.cal.list_calls >= 2  # the lookup ran before the insert


def test_ticktick_write_error_propagates_and_never_creates(tmp_path, world):
    conn = init_db(tmp_path / "e.db"); _event(conn)
    cli_mod._fan_out_confirmed(conn, _args())
    world.tt.fail_posts_with = 500
    with pytest.raises(ticktick_out.TickTickError):
        ticktick_out.upsert_event_task(conn, _row(conn), "L1")
    assert len(world.tt.tasks) == 1


def test_adoption_never_uses_the_single_task_get_that_404s_in_production(tmp_path, world):
    c1 = init_db(tmp_path / "run1.db"); _event(c1)
    cli_mod._fan_out_confirmed(c1, _args())
    c2 = init_db(tmp_path / "run2.db"); _event(c2)
    r = cli_mod._fan_out_confirmed(c2, _args())
    assert r["fanout_errors"] == 0, r["fanout_failures"]
    assert _row(c2)["ticktick_task_id"] == "t1"
    assert world.tt.single_gets == 0


def test_stored_task_no_longer_open_is_left_alone_not_recreated(tmp_path, world):
    """Completed and deleted are indistinguishable through /project/{id}/data;
    re-creating a task JP ticked off would be a duplicate."""
    conn = init_db(tmp_path / "e.db"); _event(conn)
    cli_mod._fan_out_confirmed(conn, _args())
    world.tt.tasks["t1"]["status"] = 2  # completed
    res = ticktick_out.upsert_event_task(conn, _row(conn), "L1")
    assert res.action == "unchanged" and res.task_id == "t1"
    assert len(world.tt.tasks) == 1


def test_stored_task_update_uses_the_listing_not_the_single_get(tmp_path, world):
    conn = init_db(tmp_path / "e.db"); _event(conn)
    cli_mod._fan_out_confirmed(conn, _args())
    res = ticktick_out.upsert_event_task(conn, _row(conn), "L1")
    assert res.action == "updated"
    assert world.tt.single_gets == 0


# ------------------------------------------------------------------ failure accounting

def test_all_channels_failing_is_counted_named_and_rc_unchanged_for_discover(tmp_path, world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("revoked")
    monkeypatch.setattr(gcal_out, "get_service", boom)
    monkeypatch.setattr(slack_out, "post_confirmed", boom)
    monkeypatch.setattr(ticktick_out, "find_or_create_list", boom)
    conn = init_db(tmp_path / "e.db"); _event(conn); _event(conn, ticker="MSFT")
    r = cli_mod._fan_out_confirmed(conn, _args())
    assert r["fanout_errors"] == 4  # Calendar auth x1, TickTick x1, Slack x2 rows
    joined = " | ".join(r["fanout_failures"])
    assert "Calendar auth: RuntimeError" in joined
    assert "TickTick auth/list: RuntimeError" in joined
    assert "Slack CI" in joined and "Slack MSFT" in joined
    assert "Calendar: 2 row(s) not posted" in joined
    assert "TickTick: 2 row(s) not posted" in joined


def test_no_flags_are_operator_intent_not_errors(tmp_path, world):
    conn = init_db(tmp_path / "e.db"); _event(conn)
    r = cli_mod._fan_out_confirmed(conn, _args(no_gcal=True, no_ticktick=True))
    assert r["fanout_errors"] == 0 and r["fanout_failures"] == []
    assert world.slack == ["CI"]  # no evidence either way -> ping goes out


def test_cmd_fanout_returns_1_on_failures_but_discover_summary_is_partial_not_fatal(tmp_path, world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("revoked")
    monkeypatch.setattr(gcal_out, "get_service", boom)
    db = tmp_path / "e.db"
    conn = init_db(db); _event(conn); conn.close()
    assert cli_mod.cmd_fanout(_args(db=str(db))) == 1


def test_weekly_heartbeat_reports_fanout_failures_as_partial(monkeypatch):
    posted: list = []
    monkeypatch.setattr(cli_mod.health_mod, "post_health", posted.append)
    args = argparse.Namespace(
        health_discover={"tickers_scanned": 22, "fanout_slack": 0, "fanout_errors": 2,
                         "fanout_failures": ["Calendar auth: RuntimeError: revoked",
                                             "Calendar: 1 row(s) not posted (auth failed)"],
                         "fanout_retired_matches": ["CI investor_day 2026-11-13"]},
        health_remind={}, health_digest={}, health_phase_errors=[], health_phase_rcs={},
        health_db_bootstrap=False)
    from datetime import datetime, timezone
    cli_mod._post_weekly_health(args, datetime.now(timezone.utc))
    hb = posted[0]
    assert hb.status == "partial"
    assert any(w.startswith("fan-out: 2 failure(s)") and "Calendar auth" in w for w in hb.warnings)
    assert any("auto-retired" in w and "CI investor_day" in w for w in hb.warnings)
    assert any("2 errors" in c for c in hb.counters)


def test_find_existing_event_uses_show_deleted(world):
    cal = world.cal
    row = {"ticker": "CI", "event_type": "investor_day", "start_date": START, "end_date": None,
           "company_name": "CI", "multi_day": 0, "confidence": 0.9, "status": "confirmed"}
    cal.insert("x", gcal_out._build_event_body(row, None, None))
    cal.cancel("g1")
    found = gcal_out.find_existing_event(cal, row)
    assert found is not None and found["status"] == "cancelled"
    assert gcal_out.find_existing_by_event_key(cal, row) is None  # the shim only returns LIVE ids
