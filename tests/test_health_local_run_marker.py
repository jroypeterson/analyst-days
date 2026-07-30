"""A heartbeat posted outside CI must announce that it is a local dev run.

Why this exists (2026-07-29): a local `--weekly` verification run -- the one the
`test_crash_breadcrumb_wiring` docstring describes, deliberately pointed at a
non-existent COVERAGE_MANAGER_PATH -- posted a real `:x: error` heartbeat to the
shared #status-reports channel. It was indistinguishable from a production
failure, and the next day's triage pass filed it as a live regression in the
weekly lane, complete with a claim that Friday's radar would fail the same way.
Neither was true: this project's only production paths are monday.yml and
friday.yml, and the crash came from a laptop.

The cost was a wrong entry at the top of the project board. The heartbeat is a
fleet-wide signal, so a heartbeat that lies about its own provenance is a
silent-failure bug in the reporting layer itself.

`SLACK_WEBHOOK_STATUS_REPORTS` lives in the local `.env`, so "unset webhook =>
skip posting" does NOT protect a dev run here.
"""
import os
from datetime import datetime, timezone

import pytest

from src import health


def _hb(status="error"):
    t = datetime(2026, 7, 29, 11, 50, tzinfo=timezone.utc)
    return health.Heartbeat(
        status=status, cycle="2026-07-29 weekly", start_time=t, end_time=t,
        next_expected="2026-07-31 (Friday radar)",
        warnings=["0 tickers scanned"], error_text="PHASE FAILED: discover")


def _text_of(blocks):
    return "\n".join(
        b["text"]["text"] for b in blocks if b.get("text", {}).get("text"))


@pytest.fixture
def no_ci(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def test_local_run_is_marked_in_body_and_fallback(no_ci):
    blocks, fallback = health._build_blocks(_hb())
    body = _text_of(blocks)
    assert "LOCAL" in body, body[:500]
    assert "not the scheduled CI lane" in body, body[:500]
    # The notification preview is the surface a phone reader triages from.
    assert fallback.endswith("[LOCAL]"), fallback


@pytest.mark.parametrize("var", ["CI", "GITHUB_ACTIONS"])
def test_ci_heartbeat_carries_no_local_marker(monkeypatch, no_ci, var):
    monkeypatch.setenv(var, "true")
    blocks, fallback = health._build_blocks(_hb())
    body = _text_of(blocks)
    assert "LOCAL" not in body, body[:500]
    assert "[LOCAL]" not in fallback, fallback


def test_marker_is_on_every_status_not_just_failures(no_ci):
    """An `ok` heartbeat from a laptop is the more insidious case: it can make a
    lane look alive on a day it never actually ran in CI."""
    for status in ("ok", "partial", "error"):
        blocks, fallback = health._build_blocks(_hb(status))
        assert "LOCAL" in _text_of(blocks), status
        assert fallback.endswith("[LOCAL]"), (status, fallback)


def test_marker_survives_a_long_error_text_split(no_ci):
    """`_split_long_section` chunks the body; the marker must not fall into a
    dropped or reordered chunk when the traceback is large."""
    hb = _hb()
    hb.error_text = "\n".join(f"line {i} of a very long traceback" for i in range(400))
    blocks, _ = health._build_blocks(hb)
    assert "LOCAL" in _text_of(blocks)


def test_the_marker_itself_is_ascii(no_ci):
    """cp1252 consoles kill a run before the heartbeat lands (fleet lesson), so
    text added to the payload must be ASCII.

    Scoped to the marker deliberately: the existing header carries an em-dash in
    `project — status`, which is fine because `post_health` prints the payload
    via `json.dumps` (ensure_ascii=True escapes it) and Slack receives UTF-8
    JSON. Asserting the whole payload were ASCII would be asserting a constraint
    this project has never held.
    """
    health._LOCAL_RUN_NOTE.encode("ascii", "strict")
    blocks, fallback = health._build_blocks(_hb())
    assert "[LOCAL]" in fallback
    fallback.replace("—", "-").encode("ascii", "strict")


def test_is_ci_reads_both_markers(monkeypatch, no_ci):
    assert health._is_ci() is False
    monkeypatch.setenv("CI", "true")
    assert health._is_ci() is True
