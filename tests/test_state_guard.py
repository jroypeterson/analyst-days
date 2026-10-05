"""Board #456: a missing events-DB artifact must FAIL LOUDLY unless this run was
explicitly dispatched as a bootstrap -- and the fix must hold on the shape
production has today (artifact present, 55 rows) with no action by the owner.

Three layers, each pinned here: the predicate (`src/state_guard.py`), the CLI
wiring (`cmd_weekly` / `cmd_discover` refuse BEFORE anything can create the
file), and the workflow wiring (the guard step sits between restore and run,
`Save events database` is gated on it, and the bootstrap flag can only come
from a `workflow_dispatch` input).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src import cli as cli_mod
from src import state_guard as sg
from src.state.events_repo import CandidateEvent, CandidateSource, upsert_event
from src.state.schema import init_db

CI = {"GITHUB_ACTIONS": "true"}
LOCAL: dict = {}


def _db_with_rows(path: Path, n: int = 1) -> Path:
    conn = init_db(path)
    for i in range(n):
        upsert_event(conn, CandidateEvent(
            ticker=f"T{i}", company_name="X", event_type="investor_day",
            start_date="2030-01-15", confidence=0.95, date_grounded=True,
            sources=[CandidateSource(source_type="8K", source_url="https://sec.gov/x")]),
            today_iso="2029-01-01")
    conn.close()
    return path


# ------------------------------------------------------------------ predicate

def test_production_shape_passes_without_any_flag(tmp_path):
    """Artifact present with events -> no bootstrap needed, nothing for JP to set."""
    db = _db_with_rows(tmp_path / "events.db", 3)
    assert sg.require_state(db, env=CI) is False


def test_missing_db_under_ci_refuses_and_does_not_create_the_file(tmp_path):
    db = tmp_path / "data" / "events.db"
    with pytest.raises(sg.MissingStateError) as ei:
        sg.require_state(db, env=CI)
    assert not db.exists(), "the guard must never create the file it checks for"
    assert "bootstrap_db=true" in str(ei.value)
    assert "90-day" in str(ei.value)


def test_schema_only_db_is_missing_state(tmp_path):
    """The exact shape a silent reset produces: a real SQLite file, zero events."""
    db = tmp_path / "events.db"
    init_db(db).close()
    assert db.stat().st_size > 0
    with pytest.raises(sg.MissingStateError) as ei:
        sg.require_state(db, env=CI)
    assert "0 events rows" in str(ei.value)


def test_conferences_only_db_is_missing_state(tmp_path):
    db = tmp_path / "events.db"
    conn = init_db(db)
    conn.execute(
        "INSERT INTO conferences(short_name, name, start_date, end_date) "
        "VALUES('JPM', 'J.P. Morgan Healthcare', '2027-01-11', '2027-01-14')")
    conn.commit(); conn.close()
    with pytest.raises(sg.MissingStateError):
        sg.require_state(db, env=CI)


def test_bootstrap_flag_permits_a_fresh_start_only_when_state_is_absent(tmp_path):
    db = tmp_path / "events.db"
    assert sg.require_state(db, env={**CI, sg.BOOTSTRAP_ENV: " TRUE "}) is True
    assert not db.exists()  # permitting is not creating
    _db_with_rows(db)
    # present + flag -> uses the restored DB, is NOT a bootstrap
    assert sg.require_state(db, env={**CI, sg.BOOTSTRAP_ENV: "true"}) is False


@pytest.mark.parametrize("value", ["", "false", "1", "yes", "True "[:-1] + "x"])
def test_only_the_literal_true_bootstraps(tmp_path, value):
    with pytest.raises(sg.MissingStateError):
        sg.require_state(tmp_path / "events.db", env={**CI, sg.BOOTSTRAP_ENV: value})


def test_local_dev_without_ci_is_never_blocked(tmp_path):
    assert sg.require_state(tmp_path / "events.db", env=LOCAL) is False


def test_corrupt_file_reads_as_missing_not_as_a_crash(tmp_path):
    db = tmp_path / "events.db"
    db.write_bytes(b"not a sqlite file" * 100)
    assert sg.events_row_count(db) is None
    with pytest.raises(sg.MissingStateError):
        sg.require_state(db, env=CI)


def test_record_bootstrap_keeps_the_first_stamp(tmp_path):
    conn = init_db(tmp_path / "e.db")
    assert sg.bootstrapped_at(conn) is None
    first = sg.record_bootstrap(conn, "2026-11-02T12:15:00+00:00")
    second = sg.record_bootstrap(conn, "2027-01-04T12:15:00+00:00")
    assert first == second == "2026-11-02T12:15:00+00:00"
    assert sg.bootstrapped_at(conn) == first


# ------------------------------------------------------------------ CLI entry (what the workflow runs)

def _run_guard(tmp_path, db: Path, env_extra: dict) -> subprocess.CompletedProcess:
    import os
    env = {**os.environ, **env_extra}
    env.pop("ANALYST_DAYS_BOOTSTRAP_DB", None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "src.state_guard", "--db", str(db),
         "--crash-file", str(tmp_path / ".health" / "crash.txt")],
        cwd=tmp_path, env={**env, "PYTHONPATH": str(REPO)},
        capture_output=True, text=True, timeout=60)


def test_module_entry_exit_1_and_crash_breadcrumb_when_missing(tmp_path):
    r = _run_guard(tmp_path, tmp_path / "data" / "events.db", {})
    assert r.returncode == 1
    assert "::error::" in r.stderr
    crash = tmp_path / ".health" / "crash.txt"
    assert crash.exists() and "bootstrap_db=true" in crash.read_text(encoding="utf-8")
    assert not (tmp_path / "data" / "events.db").exists()


def test_module_entry_exit_0_on_present_state(tmp_path):
    db = _db_with_rows(tmp_path / "events.db")
    r = _run_guard(tmp_path, db, {})
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / ".health" / "crash.txt").exists()


def test_module_entry_exit_0_on_bootstrap(tmp_path):
    r = _run_guard(tmp_path, tmp_path / "events.db", {"ANALYST_DAYS_BOOTSTRAP_DB": "true"})
    assert r.returncode == 0, r.stderr
    assert "BOOTSTRAP" in r.stdout


# ------------------------------------------------------------------ cmd_weekly / cmd_discover wiring

@pytest.fixture
def posted(monkeypatch):
    seen: list = []
    monkeypatch.setattr(cli_mod.health_mod, "post_health", seen.append)
    return seen


@pytest.fixture
def phases_record_db_presence(monkeypatch):
    """Every phase records whether the DB file existed when it was entered."""
    seen: list[tuple[str, bool]] = []

    def make(name):
        def phase(args):
            seen.append((name, Path(args.db).exists()))
            return 0
        return phase
    for name, fn in (("discover", "cmd_discover"), ("remind", "cmd_remind"),
                     ("digest", "cmd_monday_digest"), ("conferences", "cmd_conferences_digest")):
        monkeypatch.setattr(cli_mod, fn, make(name))
    return seen


def _args(db, **over):
    base = dict(dry_run=False, db=str(db), no_slack=True)
    base.update(over)
    return argparse.Namespace(**base)


def test_weekly_refuses_before_any_phase_when_state_is_missing(tmp_path, monkeypatch, posted,
                                                                 phases_record_db_presence):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv(sg.BOOTSTRAP_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "data" / "events.db"
    with pytest.raises(sg.MissingStateError):
        cli_mod.cmd_weekly(_args(db))
    assert phases_record_db_presence == [], "no phase may run on missing state"
    assert not db.exists()
    # The heartbeat still fired (finally) and says the phases were not reached.
    assert len(posted) == 1
    hb = posted[0]
    assert hb.status == "error"
    assert "phases not reached" in hb.error_text
    assert "STATE GUARD" in hb.error_text


def test_weekly_bootstrap_creates_db_stamps_it_and_heartbeats_partial(tmp_path, monkeypatch, posted,
                                                                       phases_record_db_presence):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv(sg.BOOTSTRAP_ENV, "true")
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "data" / "events.db"
    rc = cli_mod.cmd_weekly(_args(db))
    assert rc == 0
    assert [name for name, _ in phases_record_db_presence] == list(cli_mod.WEEKLY_PHASES)
    assert all(present for _, present in phases_record_db_presence)
    conn = init_db(db)
    assert sg.bootstrapped_at(conn) is not None
    conn.close()
    hb = posted[0]
    assert hb.status == "partial"
    assert any("BOOTSTRAPPED" in w for w in hb.warnings)


def test_weekly_on_present_state_is_an_ordinary_ok_run(tmp_path, monkeypatch, posted,
                                                       phases_record_db_presence):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv(sg.BOOTSTRAP_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    db = _db_with_rows(tmp_path / "data" / "events.db")
    # cmd_discover is stubbed, so counters are empty -> the only partial would be
    # "0 tickers scanned"; what matters is that NO bootstrap warning appears and
    # no bootstrap stamp was written.
    assert cli_mod.cmd_weekly(_args(db)) == 0
    conn = init_db(db)
    assert sg.bootstrapped_at(conn) is None
    conn.close()
    assert not any("BOOTSTRAPPED" in w for w in posted[0].warnings)


def test_discover_refuses_on_missing_state_before_opening_the_db(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv(sg.BOOTSTRAP_ENV, raising=False)
    monkeypatch.setattr(cli_mod, "load_core_watchlist", lambda: [])
    monkeypatch.setattr(cli_mod, "get_client", lambda: object())
    db = tmp_path / "events.db"
    with pytest.raises(sg.MissingStateError):
        cli_mod.cmd_discover(_args(db, tickers=None, limit=0, lookback=14))
    assert not db.exists()


# ------------------------------------------------------------------ workflow wiring

def _monday():
    return yaml.safe_load((REPO / ".github/workflows/monday.yml").read_text(encoding="utf-8"))


def _steps():
    return _monday()["jobs"]["weekly"]["steps"]


def test_guard_step_sits_between_restore_and_run():
    names = [s["name"] for s in _steps()]
    i_restore = names.index("Restore events database")
    i_guard = names.index("Require events database")
    i_run = names.index("Run weekly (discover -> remind -> Monday digest)")
    assert i_restore < i_guard < i_run
    guard = _steps()[i_guard]
    assert guard["id"] == "require_db"
    assert guard["run"].strip() == "python -m src.state_guard --db data/events.db"
    assert guard["env"]["ANALYST_DAYS_BOOTSTRAP_DB"] == "${{ github.event.inputs.bootstrap_db }}"
    assert "if" not in guard, "the guard must run unconditionally"


def test_bootstrap_flag_comes_only_from_a_dispatch_input():
    wf = _monday()
    on = wf[True]  # PyYAML parses the bare `on:` key as boolean True
    assert "bootstrap_db" in on["workflow_dispatch"]["inputs"]
    assert on["workflow_dispatch"]["inputs"]["bootstrap_db"]["default"] == "false"
    assert on["schedule"] == [{"cron": "13 12 * * 1"}]
    text = (REPO / ".github/workflows/monday.yml").read_text(encoding="utf-8")
    # Every mapping of the env var is from the input, never a var/secret/literal.
    import re
    for m in re.finditer(r"ANALYST_DAYS_BOOTSTRAP_DB:\s*(.+)", text):
        assert m.group(1).strip() == "${{ github.event.inputs.bootstrap_db }}"
    assert "vars." not in text or "BOOTSTRAP" not in text.split("vars.")[1][:40]
    run_step = next(s for s in _steps() if s.get("id") == "run_weekly")
    assert run_step["env"]["ANALYST_DAYS_BOOTSTRAP_DB"] == "${{ github.event.inputs.bootstrap_db }}"


def test_save_is_gated_on_the_guard_and_not_on_dry_run():
    save = next(s for s in _steps() if s["name"] == "Save events database")
    cond = save["if"]
    assert "steps.require_db.outcome == 'success'" in cond
    assert "github.event.inputs.dry_run != 'true'" in cond
    assert cond.startswith("always()")
    assert save["with"]["retention-days"] == 90  # the repo maximum; the guard is what covers expiry


def test_fallback_heartbeat_names_the_guard_when_it_failed():
    fb = next(s for s in _steps() if s["name"] == "Health heartbeat fallback")
    assert fb["env"]["REQUIRE_DB_OUTCOME"] == "${{ steps.require_db.outcome }}"
    assert "Require events database" in fb["run"]
    assert '"failing step: " + $step' in fb["run"]


def test_restore_still_warns_not_fails_so_the_guard_is_the_single_decision_point():
    restore = next(s for s in _steps() if s["name"] == "Restore events database")
    assert restore["with"]["if_no_artifact_found"] == "warn"
