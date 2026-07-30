"""Tests for the health/v1 heartbeat builder + poster."""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest

from src import health


@pytest.fixture(autouse=True)
def _production_shape(monkeypatch):
    """Pin these tests to the CI/production heartbeat shape.

    Added 2026-07-29 with the local-run marker: a non-CI heartbeat now carries a
    `LOCAL` marker and a `[LOCAL]` fallback suffix, so this module's exact-string
    assertions would otherwise pass or fail depending on whether the suite ran on
    a laptop or in Actions. The local shape is covered by
    `test_health_local_run_marker.py`.
    """
    monkeypatch.setenv("CI", "true")


def _hb(**overrides):
    base = dict(
        status="ok",
        cycle="2026-06-30 weekly",
        start_time=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 6, 30, 12, 4, tzinfo=timezone.utc),
        next_expected="2026-07-03 (Friday radar)",
        counters=["22 tickers · 5 hits", "2 new · 1 merged", "3 reminders · 0 errors"],
    )
    base.update(overrides)
    return health.Heartbeat(**base)


def test_blocks_are_section_mrkdwn_with_status_emoji():
    blocks, fallback = health._build_blocks(_hb())
    assert blocks and all(b["type"] == "section" for b in blocks)
    assert all(b["text"]["type"] == "mrkdwn" for b in blocks)
    head = blocks[0]["text"]["text"]
    assert ":white_check_mark:" in head and "health/v1" in head
    assert "*Counters:*" in head or any("*Counters:*" in b["text"]["text"] for b in blocks)
    assert fallback == "analyst-days — ok (2026-06-30 weekly)"


def test_partial_renders_warning_emoji_and_warnings():
    blocks, _ = health._build_blocks(
        _hb(status="partial", warnings=["3 discovery source errors"])
    )
    joined = "\n".join(b["text"]["text"] for b in blocks)
    assert ":warning:" in joined
    assert "3 discovery source errors" in joined


def test_error_includes_error_code_block():
    blocks, _ = health._build_blocks(
        _hb(status="error", error_text="Traceback: boom")
    )
    joined = "\n".join(b["text"]["text"] for b in blocks)
    assert ":x:" in joined
    assert "*Error:*" in joined and "boom" in joined


def test_long_error_splits_under_section_limit():
    big = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    blocks = health._split_long_section(big)
    assert len(blocks) > 1
    assert all(len(b["text"]["text"]) <= health.SLACK_SECTION_LIMIT for b in blocks)


def test_long_error_text_splits_rather_than_truncating():
    """Guards the rendering of the real tracebacks the weekly heartbeat now
    carries: a >3000-char error block must SPLIT, never lose its tail."""
    big = "\n".join(f'  File "frame{i}.py", line {i}, in phase' for i in range(300))
    blocks, _ = health._build_blocks(_hb(status="error", error_text=big))
    assert all(len(b["text"]["text"]) <= health.SLACK_SECTION_LIMIT for b in blocks)
    joined = "\n".join(b["text"]["text"] for b in blocks)
    assert "frame0.py" in joined and "frame299.py" in joined


def test_error_block_is_suppressed_when_status_is_ok():
    """`ok` heartbeats must not carry an error block even if error_text leaks in."""
    blocks, _ = health._build_blocks(_hb(status="ok", error_text="stale traceback"))
    joined = "\n".join(b["text"]["text"] for b in blocks)
    assert "*Error:*" not in joined


def test_local_post_without_webhook_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.delenv("SLACK_WEBHOOK_STATUS_REPORTS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.chdir(tmp_path)
    # Should log + write fallback + return (no raise) when run locally.
    health.post_health(_hb())
    assert (tmp_path / ".health" / "last_run.json").exists()
    assert not (tmp_path / ".health" / "posted").exists()


def test_ci_without_webhook_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("SLACK_WEBHOOK_STATUS_REPORTS", raising=False)
    monkeypatch.setenv("CI", "true")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError):
        health.post_health(_hb())


def test_weekly_status_zero_tickers_is_partial(monkeypatch):
    """Abnormal-counts rule (HEALTH_REPORTING.md 4.2): a 0-ticker scan means
    the watchlist load failed / discovery never ran -- never `ok`."""
    import argparse

    from src import cli as cli_mod

    captured = {}
    monkeypatch.setattr(cli_mod.health_mod, "post_health",
                        lambda hb: captured.setdefault("hb", hb))
    args = argparse.Namespace(health_discover={"tickers_scanned": 0},
                              health_remind={}, health_digest={})
    cli_mod._post_weekly_health(args, datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc))
    hb = captured["hb"]
    assert hb.status == "partial"
    assert any("0 tickers scanned" in w for w in hb.warnings)


def test_weekly_status_normal_scan_stays_ok(monkeypatch):
    import argparse

    from src import cli as cli_mod

    captured = {}
    monkeypatch.setattr(cli_mod.health_mod, "post_health",
                        lambda hb: captured.setdefault("hb", hb))
    args = argparse.Namespace(
        health_discover={"tickers_scanned": 22, "edgar_hits_total": 3,
                         "events_inserted": 1},
        health_remind={"t30": 1}, health_digest={})
    cli_mod._post_weekly_health(args, datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc))
    hb = captured["hb"]
    assert hb.status == "ok"
    assert not hb.warnings
