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


def test_explicit_trailing_year_overrides_stray_nearby_year():
    # A month/day mention that carries its OWN explicit (different) year must not
    # ground just because the target year appears nearby in another phrase.
    # Here "September 15, 2025" is a past replay reference; the stray "2026" in
    # "its 2026 Investor Day" sits within the proximity window but must NOT
    # ground 2026-09-15. (Guards the documented month/day rule, Codex High #2.)
    text = (
        "Acme announced its 2026 Investor Day. Replay materials from last "
        "year's Investor Day on September 15, 2025 are available."
    )
    assert date_grounded_in_text("2026-09-15", text) is False
    # Sanity: the SAME text with the correct trailing year does ground.
    text_ok = (
        "Acme announced its 2026 Investor Day. Details for the Investor Day "
        "on September 15, 2026 are available."
    )
    assert date_grounded_in_text("2026-09-15", text_ok) is True


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


def test_ambiguous_numeric_does_not_ground_day_first():
    # "4/3/2026" in US/SEC text means April 3. It must NOT ground 2026-03-04
    # (the day-first reading) — both components are <=12, so day-first is
    # suppressed. (Codex High: D/M/Y vs M/D/Y ambiguity.)
    assert date_grounded_in_text("2026-03-04", "Analyst Day on 4/3/2026 at 9am ET") is False
    # The M/D/Y reading of the SAME string still grounds its true date.
    assert date_grounded_in_text("2026-04-03", "Analyst Day on 4/3/2026 at 9am ET") is True


def test_unambiguous_day_first_still_grounds():
    # When the day exceeds 12 there is no M/D/Y misreading, so the day-first
    # numeric form is still honored: "15/3/2026" grounds 2026-03-15.
    assert date_grounded_in_text("2026-03-15", "the event is on 15/3/2026") is True
    assert date_grounded_in_text("2026-03-15", "the event is on 15-3-2026") is True


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
