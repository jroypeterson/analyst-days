"""Tests for the health/v1 heartbeat builder + poster."""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest

from src import health


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
