"""Deterministic date-grounding gate.

A precise extracted date should only auto-confirm if that date actually appears
— in some recognizable textual form — in the *raw source text* the classifier
read (the EDGAR excerpt / Tavily snippet), NOT the model's own rationale. This
guards against the failure the per-type confidence bar does NOT catch: the
classifier extracting a *wrong date* from a *real* announcement (e.g. reading
"September 12" but emitting 2026-09-21). High LLM self-confidence does not
protect against that — the model is confident in a date it transcribed wrong.

Conservative by design: when in doubt we report "not grounded", which keeps the
event `tentative` (radar-only) rather than auto-confirming it onto the calendar.
A false-negative costs a manual confirm; a false-positive costs wrong-day prep —
and prep on the wrong day is the expensive error this whole tool exists to avoid.

Matching is intentionally narrow: we render the ISO date into the textual forms
a press release or 8-K actually uses (long month, abbreviations, day-first,
numeric M/D/Y, ISO) and look for any of them with word-boundary anchoring.
Day-without-year forms ("September 12") only count if the 4-digit year also
appears in the text, so we don't ground 2026-09-12 on a "September 12, 2025"
mention.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional

# month number -> the spellings that appear in filings/press releases
_MONTHS: dict[int, list[str]] = {
    1: ["january", "jan"],
    2: ["february", "feb"],
    3: ["march", "mar"],
    4: ["april", "apr"],
    5: ["may"],
    6: ["june", "jun"],
    7: ["july", "jul"],
    8: ["august", "aug"],
    9: ["september", "sept", "sep"],
    10: ["october", "oct"],
    11: ["november", "nov"],
    12: ["december", "dec"],
}


def _normalize(text: str) -> str:
    """Lowercase, drop commas/periods, normalize unicode dashes, collapse space."""
    text = text.lower()
    text = re.sub(r"[‐-―−]", "-", text)  # ‐-―, minus → hyphen
    text = re.sub(r"[.,]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _ordinal(day: int) -> str:
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _date_forms(y: int, m: int, d: int) -> tuple[list[str], list[str]]:
    """Return (forms_that_include_the_year, month+day_forms_without_year).

    All forms are already normalized (lowercase, no punctuation, single-spaced)
    so they can be matched directly against _normalize(text).
    """
    months = _MONTHS[m]
    day_variants = {str(d), _ordinal(d)}
    yy = y % 100

    with_year: list[str] = []
    month_day: list[str] = []

    for mon in months:
        for dv in day_variants:
            # "september 12 2026" / day-first "12 september 2026"
            with_year.append(f"{mon} {dv} {y}")
            with_year.append(f"{dv} {mon} {y}")
            with_year.append(f"{dv} of {mon} {y}")
            # year-less (only honored if the year appears elsewhere in the text)
            month_day.append(f"{mon} {dv}")
            month_day.append(f"{dv} {mon}")

    # Numeric M/D/Y and D/M/Y, padded + unpadded, '/' and '-' separators,
    # plus ISO. These all carry the year, so they go in with_year.
    for sep in ("/", "-"):
        with_year.append(f"{m}{sep}{d}{sep}{y}")
        with_year.append(f"{m:02d}{sep}{d:02d}{sep}{y}")
        with_year.append(f"{m}{sep}{d}{sep}{yy:02d}")
        with_year.append(f"{m:02d}{sep}{d:02d}{sep}{yy:02d}")
        with_year.append(f"{d}{sep}{m}{sep}{y}")          # day-first numeric
        with_year.append(f"{d:02d}{sep}{m:02d}{sep}{y}")
    with_year.append(f"{y}-{m:02d}-{d:02d}")              # ISO
    with_year.append(f"{y}/{m:02d}/{d:02d}")

    return with_year, month_day


# How close (in characters) a 4-digit year must sit to a month/day mention for
# the month/day to count as grounded. Wide enough to catch "2026 Investor Day on
# September 15" (year leads the phrase) but tight enough to exclude a stray year
# from a filing date / copyright / unrelated period elsewhere in the text.
_YEAR_PROXIMITY = 80


def _find(norm_text: str, form: str) -> Optional[re.Match]:
    return re.search(r"\b" + re.escape(form) + r"\b", norm_text)


def _year_near(norm_text: str, year: int, span: tuple[int, int]) -> bool:
    """True if `year` appears as its own token within _YEAR_PROXIMITY chars of
    the [start, end) match span."""
    lo = max(0, span[0] - _YEAR_PROXIMITY)
    hi = min(len(norm_text), span[1] + _YEAR_PROXIMITY)
    return re.search(r"\b" + str(year) + r"\b", norm_text[lo:hi]) is not None


def date_grounded_in_text(iso_date: str, text: Optional[str]) -> bool:
    """True if `iso_date` (YYYY-MM-DD) appears, in any recognizable textual form,
    in `text`. A year-bearing form (long/numeric/ISO) grounds directly. A
    year-less month/day form ("September 15") grounds only if the matching year
    appears NEAR it — not merely somewhere in the text — so a stray year from a
    filing date or copyright can't ground an unrelated month/day.
    """
    if not iso_date or not text:
        return False
    try:
        dt = date.fromisoformat(iso_date)
    except ValueError:
        return False

    norm = _normalize(text)
    if not norm:
        return False

    with_year, month_day = _date_forms(dt.year, dt.month, dt.day)
    for form in with_year:
        if _find(norm, form):
            return True
    for form in month_day:
        for m in re.finditer(r"\b" + re.escape(form) + r"\b", norm):
            # An explicit 4-digit year *immediately* following the month/day is
            # authoritative for THIS mention. If it differs from the target
            # year, this occurrence is a different-year date (e.g. a
            # "September 15, 2025" replay reference) — skip it, so a stray
            # target-year token elsewhere within the proximity window can't
            # ground it. (A matching trailing year would already have grounded
            # via a with_year form above.) With no adjacent year we fall back to
            # the proximity heuristic, as before.
            trailing = re.match(r"\s+(\d{4})\b", norm[m.end():])
            if trailing and int(trailing.group(1)) != dt.year:
                continue
            if _year_near(norm, dt.year, m.span()):
                return True
    return False


def event_date_grounded(start_date: Optional[str], text: Optional[str]) -> bool:
    """Grounding decision for one event against its source `text`.

    Grounds strictly on the *start* date — the prep-driving day and the dedup
    key. We do NOT accept the end date as a substitute: a source phrased
    "Sept 15-16, 2026" still grounds via the start day 15, but a classifier that
    emitted the wrong start (14) with a right end (16) must NOT confirm.
    """
    return bool(start_date) and date_grounded_in_text(start_date, text)


def grounded_in_any(
    start_date: Optional[str],
    texts: Iterable[Optional[str]],
) -> bool:
    """True if the event start date is grounded in ANY of the supplied texts."""
    return any(event_date_grounded(start_date, t) for t in texts)
