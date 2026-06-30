"""Tests for the deterministic date-grounding gate."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest

from src.discovery.date_grounding import (
    date_grounded_in_text,
    event_date_grounded,
    grounded_in_any,
)


@pytest.mark.parametrize(
    "text",
    [
        "Acme will host its Investor Day on September 15, 2026 at 8:30am ET.",
        "Investor Day — Sept 15, 2026 — webcast registration open.",
        "the company's R&D Day on 9/15/2026 in Boston",
        "Capital Markets Day 09/15/2026",
        "save the date: 15 September 2026 for our analyst day",
        "the two-day event begins September 15th, 2026",
        "ISO-style 2026-09-15 appears in this machine-readable feed",
    ],
)
def test_grounds_common_date_renderings(text):
    assert date_grounded_in_text("2026-09-15", text) is True


def test_year_required_for_monthday_only_mention():
    # Month+day present but a DIFFERENT year -> must NOT ground 2026.
    assert date_grounded_in_text(
        "2026-09-15", "held on September 15, 2025 last year"
    ) is False
    # Month+day with no year at all -> not grounded (conservative).
    assert date_grounded_in_text("2026-09-15", "around September 15 sometime") is False


def test_year_leading_phrase_grounds_monthday():
    # Common filing phrasing: year leads the event name, date follows.
    assert date_grounded_in_text(
        "2026-09-15", "host its 2026 Investor Day on September 15 at HQ"
    ) is True


def test_distant_stray_year_does_not_ground_monthday():
    # Year is far from the month/day (e.g. a copyright / filing-date artifact).
    text = "Investor Day September 15. " + ("filler " * 30) + "Copyright 2026 Acme Corp."
    assert date_grounded_in_text("2026-09-15", text) is False


def test_does_not_ground_wrong_date():
    # Source says the 12th; classifier emitted the 21st -> the exact failure
    # mode this gate exists to catch.
    text = "Investor Day on September 12, 2026 at HQ"
    assert date_grounded_in_text("2026-09-21", text) is False
    assert date_grounded_in_text("2026-09-12", text) is True


def test_day_number_not_substring_matched():
    # "September 1" must not ground on "September 15".
    assert date_grounded_in_text("2026-09-01", "Investor Day September 15, 2026") is False


def test_empty_and_malformed_inputs():
    assert date_grounded_in_text("2026-09-15", "") is False
    assert date_grounded_in_text("2026-09-15", None) is False
    assert date_grounded_in_text("", "September 15, 2026") is False
    assert date_grounded_in_text("not-a-date", "September 15, 2026") is False


def test_multiday_range_grounds_via_start_day():
    # "September 15-16, 2026" -> start day 15 is present, so it grounds.
    assert event_date_grounded("2026-09-15", "Investor Day September 15-16, 2026") is True


def test_end_date_alone_does_not_ground_wrong_start():
    # Source proves only the END day; a wrong START must NOT confirm.
    text = "the two-day investor day concludes on September 16, 2026"
    assert event_date_grounded("2026-09-14", text) is False


def test_grounded_in_any_scans_all_texts():
    texts = ["nothing relevant here", "Investor Day September 15, 2026 confirmed"]
    assert grounded_in_any("2026-09-15", texts) is True
    assert grounded_in_any("2026-09-15", ["nope", "still nope"]) is False
