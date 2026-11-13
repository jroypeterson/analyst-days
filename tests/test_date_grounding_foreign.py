"""Dates as non-English investor pages write them (2026-11-13).

The source is read in its own language; nothing is translated, because a translation is
model-written text and the gate checks a date against text nobody generated.
"""
import pytest

from src.discovery.date_grounding import date_grounded_in_text as grounded
from src.discovery.web_answers import cut_snippet


@pytest.mark.parametrize("iso, text", [
    # Japanese / Chinese / Korean
    ("2026-12-11", "キャピタル・マーケッツ・デー 2026年12月11日（金）開催"),
    ("2026-12-11", "開催日：２０２６年１２月１１日"),                 # full-width digits
    ("2026-03-05", "投资者日将于2026年3月5日举行"),
    ("2027-03-05", "2027년 3월 5일 투자자의 날"),
    ("2026-12-11", "IR Day 2026.12.11"),
    # German
    ("2026-12-11", "Kapitalmarkttag am 11. Dezember 2026 in Frankfurt"),
    ("2026-12-11", "Capital Markets Day, 11.12.2026"),
    ("2027-03-05", "Kapitalmarkttag: 05.03.2027"),
    ("2027-03-05", "am 5. März 2027"),
    # French
    ("2026-12-11", "Journée investisseurs le 11 décembre 2026 à Paris"),
    ("2027-03-01", "Journée investisseurs le 1er mars 2027"),
    # Spanish / Portuguese / Italian
    ("2026-12-11", "Día del Inversor: 11 de diciembre de 2026"),
    ("2027-03-05", "Dia do Investidor em 5 de março de 2027"),
    ("2026-12-11", "Capital Markets Day l'11 dicembre 2026".replace("l'11", "il 11")),
    # Nordic / Dutch
    ("2026-12-11", "Kapitalmarkedsdag den 11. december 2026".replace("december", "desember")),
    ("2027-03-05", "Capital Markets Day op 5 maart 2027"),
])
def test_a_foreign_language_date_grounds(iso, text):
    assert grounded(iso, text)


@pytest.mark.parametrize("iso, text", [
    ("2026-12-11", "2026年12月10日"),                         # the day before
    ("2026-12-11", "2025年12月11日"),                         # last year's event
    ("2026-12-11", "2026年11月12日"),                         # month and day swapped
    ("2026-12-01", "2026年12月11日"),                         # 1 must not match inside 11
    ("2026-12-11", "12026年12月11日"),                        # not inside a longer number
    ("2026-12-11", "am 10. Dezember 2026"),
    ("2026-12-11", "am 11. Dezember 2025"),
    ("2026-12-11", "11.12.2025"),
    ("2026-12-11", "12.11.2026"),                             # 12 November, day-first
    ("2026-12-11", "111.12.2026"),
    ("2026-12-01", "le 11 décembre 2026"),
    ("2026-12-11", "le 11 décembre"),                         # no year: not accepted
    ("2026-12-11", "version 11.12.20261"),
])
def test_a_different_date_does_not_ground(iso, text):
    assert not grounded(iso, text)


def test_english_grounding_is_unchanged():
    assert grounded("2026-12-10", "Investor Day on December 10, 2026")
    assert not grounded("2026-12-10", "Investor Day on December 11, 2026")
    assert not grounded("2026-03-04", "4/3/2026")             # ambiguous US numeric stays refused


def test_a_page_with_no_english_event_name_still_yields_the_dated_text():
    """Takeda's page: the event is named in Japanese, so no English trigger matches."""
    page = ("武田薬品 IRイベント " + "説明 " * 300 +
            "キャピタル・マーケッツ・デー 2026年12月11日 開催 " + "その他 " * 100)
    snippet = cut_snippet(page, "2026-12-11")
    assert "2026年12月11日" in snippet and grounded("2026-12-11", snippet)
    assert cut_snippet(page, "2026-12-10") == ""              # the wrong date finds nothing
