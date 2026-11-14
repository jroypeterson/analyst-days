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
import unicodedata
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


# ── dates as non-English sources write them (2026-11-13) ─────────────────────
#
# 58 scan-set names file nothing with the SEC, and their investor pages are often not in
# English: Takeda's events page states its Capital Markets Day as 2026年12月11日. JP:
# "For foreign language ones why don't you translate and read the source?" The source IS
# read -- in its own language, by these patterns -- rather than through a translation,
# because a translation is text a model wrote, and this gate exists to check a date
# against text nobody generated.
#
# Year-bearing forms only. A year-less "11 décembre" is not accepted: the English path
# needs a nearby year for that, and widening it here is how a wrong year would ground.
_FOREIGN_MONTHS: dict[int, list[str]] = {
    1: ["januar", "janvier", "enero", "gennaio", "janeiro", "januari"],
    2: ["februar", "février", "fevrier", "febrero", "febbraio", "fevereiro", "februari"],
    3: ["märz", "maerz", "mars", "marzo", "março", "marco", "maart", "marts"],
    4: ["avril", "abril", "aprile"],
    5: ["mai", "mayo", "maggio", "maio", "mei", "maj"],
    6: ["juni", "juin", "junio", "giugno", "junho"],
    7: ["juli", "juillet", "julio", "luglio", "julho"],
    8: ["août", "aout", "agosto", "augustus", "augusti"],
    9: ["septembre", "septiembre", "setiembre", "settembre", "setembro"],
    10: ["oktober", "octobre", "octubre", "ottobre", "outubro"],
    11: ["novembre", "noviembre", "novembro"],
    12: ["dezember", "décembre", "decembre", "diciembre", "dicembre", "dezembro", "desember"],
}


def _foreign_grounded(y: int, m: int, d: int, text: str) -> bool:
    """True if the date is written out, with its year, in a non-English form."""
    t = unicodedata.normalize("NFKC", text).lower()       # full-width digits -> ASCII
    # Japanese / Chinese 2026年12月11日, Korean 2026년 12월 11일. No \b: CJK characters
    # are word characters, so a boundary never falls between kana and a digit.
    for ys, ms, ds in (("年", "月", "日"), ("년", "월", "일")):
        if re.search(rf"(?<!\d){y}\s*{ys}\s*0?{m}\s*{ms}\s*0?{d}\s*{ds}", t):
            return True
    # Dotted numerics. With dots the order is never the US one: 11.12.2026 is day-first
    # across Europe, 2026.12.11 year-first in East Asia.
    if re.search(rf"(?<![\d.]){d:02d}\.{m:02d}\.{y}(?![\d])", t) or \
            re.search(rf"(?<![\d.]){d}\.{m}\.{y}(?![\d])", t) or \
            re.search(rf"(?<![\d.]){y}\.\s?{m:02d}\.\s?{d:02d}(?![\d])", t):
        return True
    # Day-first with a month name: "11. Dezember 2026", "11 décembre 2026", "1er mars 2027",
    # "11 de diciembre de 2026", "den 11 december 2026".
    day = rf"0?{d}(?:er|\.|º|°)?"
    for mon in _FOREIGN_MONTHS.get(m, []):
        if re.search(rf"(?<![\d]){day}\s+(?:de\s+|di\s+)?{re.escape(mon)}\.?,?\s+(?:de\s+|del\s+)?{y}(?!\d)", t):
            return True
    return False


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
    #
    # Day-first (D/M/Y) is emitted ONLY when it can't be misread as US-convention
    # M/D/Y — i.e. when the day exceeds 12. When BOTH month and day are <=12,
    # "4/3/2026" is genuinely ambiguous (April 3 vs 3 April), and US/SEC filings
    # overwhelmingly write M/D/Y, so honoring the day-first reading would ground
    # the wrong day (e.g. ground 2026-03-04 on "4/3/2026" that means April 3).
    # Suppressing it keeps the gate conservative — a false-negative costs a manual
    # confirm; a false-positive costs wrong-day prep.
    day_first_ambiguous = m <= 12 and d <= 12
    for sep in ("/", "-"):
        with_year.append(f"{m}{sep}{d}{sep}{y}")
        with_year.append(f"{m:02d}{sep}{d:02d}{sep}{y}")
        with_year.append(f"{m}{sep}{d}{sep}{yy:02d}")
        with_year.append(f"{m:02d}{sep}{d:02d}{sep}{yy:02d}")
        if not day_first_ambiguous:
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
    if _foreign_grounded(dt.year, dt.month, dt.day, text):
        return True

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


def grounded_in_cited_source(
    start_date: Optional[str],
    date_imprecise: bool,
    source_url: Optional[str],
    text_by_url: dict[str, str],
) -> bool:
    """The gate as discovery applies it to one classifier row: ground against the
    RAW text of the source the classifier CITED; only if that URL is not one we
    fetched, fall back to the union of the texts in the same bundle.

    Same rule as `cli._event_grounded` (which predates this and is left alone);
    it lives here so the backfill applies the production gate without importing
    `src.cli`, and with it every output channel.
    """
    if date_imprecise or not start_date:
        return False
    cited = text_by_url.get(source_url or "")
    if cited is not None:
        return event_date_grounded(start_date, cited)
    return grounded_in_any(start_date, list(text_by_url.values()))
