"""Phase isolation in `--weekly`, and the diagnostics its heartbeat carries.

Regression tests for the 2026-07-27 abort. A UTF-8 BOM on Coverage Manager's
`exports/watchlist.csv` made `row["Ticker"]` raise `KeyError('\\ufeffTicker')`
inside `cmd_discover`; that exception propagated straight out of `cmd_weekly`
and killed `cmd_remind`, `cmd_monday_digest` AND the health heartbeat with it.

`cmd_weekly`'s docstring already promised the opposite ("a failure in one is
reported but does NOT skip the rest"), but the implementation called each phase
bare, which handles a phase's RETURN CODE, not a phase EXCEPTION. A docstring
is not a mechanism -- these tests are.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest

from src import cli as cli_mod

# The exact shape of the 2026-07-27 failure: the key carries a non-ASCII BOM,
# so anything that renders it to a cp1252 console must sanitize the DATA too.
BOM_KEY = "﻿Ticker"


def _args(**over):
    base = dict(dry_run=False, db="unused.db", no_slack=True)
    base.update(over)
    return argparse.Namespace(**base)


def _raise_bom(args):
    raise KeyError(BOM_KEY)


@pytest.fixture
def posted(monkeypatch):
    """Capture heartbeats instead of posting them."""
    seen: list = []
    monkeypatch.setattr(cli_mod.health_mod, "post_health", seen.append)
    return seen


@pytest.fixture
def ok_phases(monkeypatch):
    """Stub every weekly phase to succeed; individual tests override one.

    Must cover ALL of cli_mod.WEEKLY_PHASES — an unstubbed phase runs for real
    and drags a live dependency (a webhook, the DB) into what is meant to be a
    pure isolation test. The assertion below fails loudly if a phase is added to
    cmd_weekly and not stubbed here.
    """
    calls: list[str] = []

    def make(name, rc=0):
        def phase(args):
            calls.append(name)
            return rc
        return phase

    monkeypatch.setattr(cli_mod, "cmd_discover", make("discover"))
    monkeypatch.setattr(cli_mod, "cmd_remind", make("remind"))
    monkeypatch.setattr(cli_mod, "cmd_monday_digest", make("digest"))
    monkeypatch.setattr(cli_mod, "cmd_conferences_digest", make("conferences"))
    return calls


def test_the_ok_phases_fixture_covers_every_weekly_phase(ok_phases, posted,
                                                         monkeypatch):
    """Guard the fixture itself: add a phase to cmd_weekly, stub it here too."""
    cli_mod.cmd_weekly(_args())
    assert set(ok_phases) == set(cli_mod.WEEKLY_PHASES)


# --------------------------------------------------------------------------
# Defect A -- phase isolation
# --------------------------------------------------------------------------

def test_a_raising_phase_does_not_skip_later_phases(monkeypatch, posted, ok_phases):
    """The headline regression: discover blowing up must not cost the run."""
    monkeypatch.setattr(cli_mod, "cmd_discover", _raise_bom)

    rc = cli_mod.cmd_weekly(_args())

    # Every phase after the raising one must still have run. Derived from
    # WEEKLY_PHASES rather than hardcoded so adding a phase can't quietly
    # narrow what this regression covers.
    expected = [p for p in cli_mod.WEEKLY_PHASES if p != "discover"]
    assert ok_phases == expected, (
        f"{expected} were skipped -- the phase exception propagated"
    )
    assert rc != 0, "a raising phase must still make the run exit non-zero"


def test_a_raising_phase_still_posts_a_heartbeat(monkeypatch, posted, ok_phases):
    monkeypatch.setattr(cli_mod, "cmd_discover", _raise_bom)

    cli_mod.cmd_weekly(_args())

    assert len(posted) == 1, "exactly one heartbeat per weekly run"
    hb = posted[0]
    assert hb.status == "error"
    assert "KeyError" in hb.error_text
    assert "Ticker" in hb.error_text
    assert "discover" in hb.error_text, "the heartbeat must name the failing phase"


def test_heartbeat_error_text_is_ascii_even_though_the_bom_is_not(
    monkeypatch, posted, ok_phases
):
    """ASCII console rule: sanitize the DATA, not just the format string.

    The traceback of the 2026-07-27 abort ends in `KeyError: '\\ufeffTicker'`.
    Printing that raw to a cp1252 console raises UnicodeEncodeError and kills
    the process before it can report anything.

    Scope note: the pre-existing warnings/counters use deliberate Slack
    typography (`—`, `·`) and are NOT in scope -- only the exception text this
    change newly injects into the payload is.
    """
    monkeypatch.setattr(cli_mod, "cmd_discover", _raise_bom)

    cli_mod.cmd_weekly(_args())

    hb = posted[0]
    assert hb.error_text.isascii(), f"non-ASCII in error_text: {hb.error_text!r}"
    assert "\\ufeff" in hb.error_text, "the escaped BOM is the diagnosis -- keep it"
    raised = [w for w in hb.warnings if "raised:" in w]
    assert raised and all(w.isascii() for w in raised)


def test_only_the_last_20_traceback_lines_are_carried(monkeypatch, posted, ok_phases):
    """HEALTH_REPORTING.md 4.1: error block = code block, last ~20 lines."""
    def deep(args, depth=60):
        if depth:
            return deep(args, depth - 1)
        raise KeyError(BOM_KEY)

    monkeypatch.setattr(cli_mod, "cmd_discover", deep)

    cli_mod.cmd_weekly(_args())

    body = posted[0].error_text
    # 1 banner line + at most ERROR_TAIL_LINES of traceback, per failed phase.
    assert len(body.splitlines()) <= cli_mod.ERROR_TAIL_LINES + 1
    assert "KeyError" in body, "the tail must include the exception line"


def test_a_nonzero_rc_without_an_exception_is_partial_not_error(
    monkeypatch, posted, ok_phases
):
    """A phase that reports failure by return code is degraded, not aborted."""
    def discover(args):
        args.health_discover = {"tickers_scanned": 22}
        return 0

    monkeypatch.setattr(cli_mod, "cmd_discover", discover)
    monkeypatch.setattr(cli_mod, "cmd_remind", lambda a: 1)

    rc = cli_mod.cmd_weekly(_args())

    assert rc != 0
    hb = posted[0]
    assert hb.status == "partial", "no exception was raised, so this is not `error`"
    assert any("remind" in w for w in hb.warnings)


def test_a_clean_run_is_ok(monkeypatch, posted, ok_phases):
    def discover(args):
        args.health_discover = {"tickers_scanned": 22}
        return 0

    monkeypatch.setattr(cli_mod, "cmd_discover", discover)

    rc = cli_mod.cmd_weekly(_args())

    assert rc == 0
    assert posted[0].status == "ok"
    assert not posted[0].warnings


# --------------------------------------------------------------------------
# Defect B -- the heartbeat must fire even when the isolation itself breaks
# --------------------------------------------------------------------------

def test_heartbeat_posts_even_if_isolation_helper_itself_raises(
    monkeypatch, posted, ok_phases
):
    """Proves the `finally`. If `_run_phase` is the thing that breaks, the
    operator must still get a heartbeat rather than the workflow's constant."""
    def explode(*a, **k):
        raise RuntimeError("isolation helper is itself broken")

    monkeypatch.setattr(cli_mod, "_run_phase", explode)

    with pytest.raises(RuntimeError, match="isolation helper"):
        cli_mod.cmd_weekly(_args())

    assert len(posted) == 1, "the finally must still heartbeat"
    hb = posted[0]
    assert hb.status == "error"
    assert "isolation helper" in hb.error_text
    assert "not reached" in hb.error_text, (
        "the heartbeat must say which phases never ran"
    )


def test_dry_run_still_skips_the_heartbeat(monkeypatch, posted, ok_phases):
    """Unchanged behaviour: --dry-run makes no external writes."""
    monkeypatch.setattr(cli_mod, "cmd_discover", _raise_bom)

    rc = cli_mod.cmd_weekly(_args(dry_run=True))

    assert rc != 0
    assert posted == []


# --------------------------------------------------------------------------
# Defect B -- the crash breadcrumb the workflow fallback reads
# --------------------------------------------------------------------------

def test_crash_breadcrumb_captures_an_ascii_traceback(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    try:
        raise KeyError(BOM_KEY)
    except KeyError:
        cli_mod._write_crash_breadcrumb()

    crash = tmp_path / ".health" / "crash.txt"
    assert crash.exists(), "the workflow fallback has nothing to tail without this"
    body = crash.read_text(encoding="utf-8")
    assert "KeyError" in body
    assert "Traceback" in body
    assert body.isascii(), f"non-ASCII in crash.txt: {body!r}"


def test_crash_breadcrumb_never_masks_the_original_exception(monkeypatch, tmp_path):
    """Best-effort: an unwritable .health/ must not replace the real traceback."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli_mod.Path, "mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    try:
        raise KeyError(BOM_KEY)
    except KeyError:
        cli_mod._write_crash_breadcrumb()  # must not raise


def test_main_removes_a_stale_crash_breadcrumb(monkeypatch, tmp_path):
    """A crash.txt left by a previous local run would be a false diagnosis."""
    monkeypatch.chdir(tmp_path)
    stale = tmp_path / ".health" / "crash.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale traceback from last week", encoding="utf-8")

    monkeypatch.setattr(cli_mod, "load_dotenv", lambda *a, **k: None)
    cli_mod.main(["--status", "--db", str(tmp_path / "nope.db")])

    assert not stale.exists()
