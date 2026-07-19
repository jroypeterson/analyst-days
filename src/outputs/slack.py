"""Slack output — #analyst-days channel.

Three message shapes:
  - post_confirmed(event)        Per-event ping when a discovery flips to status=confirmed.
  - post_friday_digest(rows)     Weekly Friday "what's on the radar" — all future events
                                  (confirmed + discovered + tentative) as a compact table.
  - post_monday_digest(...)      Weekly Monday "imminent" — forward 30-day + 7-day views.

All three post to SLACK_WEBHOOK_ANALYST_DAYS via Slack's incoming-webhook API
(Block Kit). Webhooks are channel-bound — there's no need to pass channel here.
"""
from __future__ import annotations

import os
import time
from datetime import date
from typing import Iterable, Optional

import requests

from src.state.events_repo import PUSHABLE_EVENT_TYPES

WEBHOOK_ENV = "SLACK_WEBHOOK_ANALYST_DAYS"

# Transient-network resilience: retry the Slack POST on momentary DNS/socket blips.
_RETRY_BACKOFF = (5, 15, 30)  # seconds to wait BEFORE retry attempts 2..N

# Friendly display names (sortable on the wire as the keys)
EVENT_TYPE_LABELS = {
    "investor_day": "Investor Day",
    "analyst_day": "Analyst Day",
    "rd_day": "R&D Day",
    "capital_markets_day": "Capital Markets Day",
    "conference": "Conference",
}

SOURCE_LABELS = {
    "8K": "8K",
    "IR_PAGE": "IR",
    "PRESS_RELEASE": "PR",
    "TAVILY_HIT": "Web",
    "MANUAL": "Man",
}

# Fuller source labels for hyperlink anchor text in section-mrkdwn blocks
# (the per-event ping + Upcoming/YTD lists). The width-constrained monospace
# tables keep the compact SOURCE_LABELS above; links can't render in a code
# block anyway. Full names per the workspace "no bare abbreviations" convention.
ANNOUNCEMENT_SOURCE_LABELS = {
    "8K": "8-K filing",
    "IR_PAGE": "IR page",
    "PRESS_RELEASE": "Press release",
    "TAVILY_HIT": "Web source",
    "MANUAL": "Manual entry",
}

# Confirmed-family statuses — a real, prep-driving event. Used by the Monday
# digest's Upcoming (forward) list.
CONFIRMED_FAMILY_STATUSES = ("confirmed", "reminded_30", "reminded_7", "day_of")
# Statuses that count as an event that actually happened, for the YTD (trailing)
# list. Adds terminal past states; excludes suspected (discovered/tentative) and
# retired (cancelled/superseded) rows so we never list an unconfirmed or called-off
# event as "held".
YTD_STATUSES = CONFIRMED_FAMILY_STATUSES + ("completed", "historical")


def _row_get(row, key):
    """sqlite3.Row raises IndexError on a missing key and has no .get(); this
    returns None instead so callers work whether or not the query selected the
    optional source columns (keeps existing tests that pass bare rows working)."""
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _source_link(row) -> Optional[str]:
    """mrkdwn hyperlink `<url|Label>` for the event's primary announcement source.

    Never fabricates a URL: if the row carries a source_type but no URL we say so
    explicitly; if there's no source info at all we return None (caller omits the
    field). Reads either `source_url`/`source_type` (per-event rows) or the
    `primary_source` alias used by the digest queries.
    """
    url = _row_get(row, "source_url")
    stype = _row_get(row, "source_type") or _row_get(row, "primary_source")
    label = ANNOUNCEMENT_SOURCE_LABELS.get(stype or "", stype or "Source")
    if url:
        safe = str(url).replace("<", "").replace(">", "").replace("|", "")
        return f"<{safe}|{label}>"
    if stype:
        return f"{label} (no link captured)"
    return None

STATUS_LABELS = {
    "confirmed": "confirmed",
    "discovered": "suspected",
    "tentative": "suspected",
    "reminded_30": "confirmed",
    "reminded_7": "confirmed",
    "day_of": "TODAY",
    "completed": "past",
    "historical": "past",
}


# ---------------------------------------------------------------------------
# Low-level webhook poster
# ---------------------------------------------------------------------------


def _webhook_url() -> str:
    url = os.environ.get(WEBHOOK_ENV, "").strip()
    if not url:
        raise RuntimeError(f"{WEBHOOK_ENV} not set")
    return url


def _post(payload: dict) -> None:
    url = _webhook_url()
    # Retry only on transient transport errors (network/DNS blip); a successful
    # POST with a bad status is NOT retried and falls through to the checks below.
    attempts = 1 + len(_RETRY_BACKOFF)
    last_exc = None
    r = None
    for i in range(attempts):
        try:
            r = requests.post(url, json=payload, timeout=15)
            break
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if i < attempts - 1:
                delay = _RETRY_BACKOFF[i]
                if not os.environ.get("PYTEST_CURRENT_TEST"):
                    time.sleep(delay)
    if r is None:
        raise last_exc
    r.raise_for_status()
    # Slack returns plain "ok" body on success
    if r.text.strip() != "ok":
        raise RuntimeError(f"Slack webhook returned: {r.text!r}")


# ---------------------------------------------------------------------------
# Per-event confirmation ping
# ---------------------------------------------------------------------------


def post_confirmed(event_row) -> None:
    """Per-event ping fired when status flips to confirmed.

    Conferences are tracked but not pushed — silently skipped here.
    `event_row` is a sqlite3.Row (or any mapping) from the events table.
    """
    e = event_row
    if e["event_type"] not in PUSHABLE_EVENT_TYPES:
        return
    type_label = EVENT_TYPE_LABELS.get(e["event_type"], e["event_type"])
    when_str = e["start_date"] or "(date TBD)"
    if e["multi_day"] and e["end_date"]:
        when_str = f"{e['start_date']} – {e['end_date']} (multi-day)"

    header = f":calendar: *New {type_label}* — `{e['ticker']}`"
    if e["company_name"]:
        header += f" ({e['company_name']})"

    fields = [
        {"type": "mrkdwn", "text": f"*Date*\n{when_str}"},
        {"type": "mrkdwn", "text": f"*Confidence*\n{e['confidence']:.2f}"},
    ]
    src_link = _source_link(e)
    if src_link:
        fields.append({"type": "mrkdwn", "text": f"*Announcement*\n{src_link}"})
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "fields": fields},
    ]

    _post({
        "text": f"New {type_label} for {e['ticker']}",
        "blocks": blocks,
    })


# ---------------------------------------------------------------------------
# Reminder pings (T-30 / T-7 / day-of)
# ---------------------------------------------------------------------------

# Lead-time label + emoji per reminder kind. Keys match reminders.REMINDER_KINDS.
REMINDER_LABELS = {
    "t30": (":hourglass_flowing_sand:", "~30 days out"),
    "t7": (":alarm_clock:", "1 week out"),
    "day_of": (":rotating_light:", "happening this week"),
}


def post_reminder(event_row, kind: str) -> None:
    """Post a single reminder ping for a confirmed event.

    `kind` is one of "t30" / "t7" / "day_of". Conferences (non-pushable)
    are silently skipped so a policy change can't leak them into the feed.
    Ticker is rendered as a backtick monospace chip (workspace convention).
    """
    e = event_row
    if e["event_type"] not in PUSHABLE_EVENT_TYPES:
        return
    emoji, lead = REMINDER_LABELS.get(kind, (":calendar:", "upcoming"))
    type_label = EVENT_TYPE_LABELS.get(e["event_type"], e["event_type"])

    when_str = e["start_date"] or "(date TBD)"
    if e["multi_day"] and e["end_date"]:
        when_str = f"{e['start_date']} - {e['end_date']} (multi-day)"

    header = f"{emoji} *Reminder ({lead})* — `{e['ticker']}` {type_label}"
    if e["company_name"]:
        header += f" ({e['company_name']})"

    fields = [
        {"type": "mrkdwn", "text": f"*Date*\n{when_str}"},
        {"type": "mrkdwn", "text": f"*Confidence*\n{e['confidence']:.2f}"},
    ]
    src_link = _source_link(e)
    if src_link:
        fields.append({"type": "mrkdwn", "text": f"*Announcement*\n{src_link}"})
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "fields": fields},
    ]
    _post({
        "text": f"Reminder ({lead}): {e['ticker']} {type_label} on {when_str}",
        "blocks": blocks,
    })


# ---------------------------------------------------------------------------
# Friday digest — full radar
# ---------------------------------------------------------------------------


def _format_row(row, today_iso: str) -> str:
    """One line in the monospace digest sub-table.

    Status column is dropped — we split into 'Confirmed' / 'Suspected'
    sub-tables in the digest, so per-row status is redundant.
    """
    ticker = (row["ticker"] or "")[:6]
    type_label = EVENT_TYPE_LABELS.get(row["event_type"], row["event_type"])[:18]

    if row["start_date"]:
        when = row["start_date"]
        if row["multi_day"] and row["end_date"]:
            when = f"{row['start_date']}+"
    elif row["imprecise_hint"]:
        when = (row["imprecise_hint"] or "")[:11]
    else:
        when = "TBD"

    conf = f"{(row['confidence'] or 0.0):.2f}"

    src_summary = ""
    try:
        src_summary = SOURCE_LABELS.get(row["primary_source"] or "", (row["primary_source"] or ""))[:4]
    except (IndexError, KeyError):
        pass

    return f"{when:11} {ticker:6}  {type_label:18}  {conf:4}  {src_summary}"


def _month_label(iso_date: str) -> str:
    from datetime import date as _d
    return _d.fromisoformat(iso_date).strftime("%B %Y")


def _grouped_table(rows, today_iso: str) -> str:
    """Code-block table with month dividers. Imprecise rows go under 'Date TBD'."""
    if not rows:
        return "_(none)_"

    header = f"{'DATE':11} {'TICKER':6}  {'TYPE':18}  {'CONF':4}  SRC"
    sep = "-" * len(header)

    precise = [r for r in rows if r["start_date"]]
    imprecise = [r for r in rows if not r["start_date"]]

    body: list[str] = []
    last_month = None
    for r in precise:
        m = _month_label(r["start_date"])
        if m != last_month:
            body.append(f"── {m} ──")
            last_month = m
        body.append(_format_row(r, today_iso))

    if imprecise:
        body.append("── Date TBD ──")
        for r in imprecise:
            body.append(_format_row(r, today_iso))

    return "```\n" + header + "\n" + sep + "\n" + "\n".join(body) + "\n```"


def _query_radar(conn, today_iso: str) -> list:
    """All future pushable events (confirmed + discovered + tentative).

    Conferences are excluded — they're tracked in the DB but the user
    explicitly opted them out of the Slack signal.
    """
    type_placeholders = ",".join(["?"] * len(PUSHABLE_EVENT_TYPES))
    pushable_types = sorted(PUSHABLE_EVENT_TYPES)
    return conn.execute(
        f"""
        SELECT
            e.id, e.ticker, e.company_name, e.event_type,
            e.start_date, e.end_date, e.multi_day,
            e.date_imprecise, e.imprecise_hint,
            e.status, e.confidence,
            (SELECT s.source_type FROM event_sources s
              WHERE s.event_id = e.id ORDER BY s.id ASC LIMIT 1) AS primary_source
        FROM events e
        WHERE e.status IN ('confirmed','discovered','tentative',
                           'reminded_30','reminded_7','day_of')
          AND e.event_type IN ({type_placeholders})
          AND (e.start_date IS NULL OR e.start_date >= ?)
        ORDER BY
          CASE WHEN e.start_date IS NULL THEN 1 ELSE 0 END,
          e.start_date ASC,
          e.ticker ASC
        """,
        (*pushable_types, today_iso),
    ).fetchall()


def post_friday_digest(conn, today_iso: Optional[str] = None) -> int:
    """Post the Friday 'on the radar' digest. Returns the number of events posted."""
    today_iso = today_iso or date.today().isoformat()
    rows = _query_radar(conn, today_iso)

    confirmed = [r for r in rows if r["status"] in
                 ("confirmed", "reminded_30", "reminded_7", "day_of")]
    suspected = [r for r in rows if r["status"] in ("discovered", "tentative")]

    summary = (
        f":calendar: *Analyst Days — Friday Radar* ({today_iso})  |  "
        f"*{len(rows)}* future events  ·  "
        f"{len(confirmed)} confirmed  ·  {len(suspected)} suspected"
    )

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
    ]
    if confirmed:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Confirmed ({len(confirmed)})*\n"
                    + _grouped_table(confirmed, today_iso)}})
    if suspected:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Suspected ({len(suspected)})*\n"
                    + _grouped_table(suspected, today_iso)}})
    if not rows:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "_(nothing on the radar — run --discover)_"}})

    blocks.append({"type": "context", "elements": [{
        "type": "mrkdwn",
        "text": "_Confirmed = single authoritative source w/ precise date "
                "≥0.80 confidence. Suspected = below threshold or imprecise date._",
    }]})

    _post({
        "text": f"Analyst Days Friday radar — {len(rows)} events",
        "blocks": blocks,
    })
    return len(rows)


# ---------------------------------------------------------------------------
# Monday digest — imminent (forward 30/7 day views)
# ---------------------------------------------------------------------------


def query_upcoming_ytd(conn, today_iso: str) -> tuple[list, list]:
    """Return (upcoming, ytd) precise-dated pushable events for the Monday digest.

    upcoming = confirmed-family events dated today-or-later (the full forward
               calendar, not just the 30-day window), ascending.
    ytd      = events that already happened this calendar year (Jan 1 .. today),
               most-recent-first. Only real (confirmed/completed/historical)
               events — suspected and retired rows are excluded.

    Conferences are excluded (tracked-only, opted out of the Slack signal).
    Imprecise-dated events (no start_date) never appear here — they live on the
    Friday radar until a precise source lands.
    """
    type_placeholders = ",".join(["?"] * len(PUSHABLE_EVENT_TYPES))
    pushable_types = sorted(PUSHABLE_EVENT_TYPES)
    src_cols = """
               (SELECT s.source_type FROM event_sources s
                 WHERE s.event_id = e.id ORDER BY s.id ASC LIMIT 1) AS source_type,
               (SELECT s.source_url FROM event_sources s
                 WHERE s.event_id = e.id ORDER BY s.id ASC LIMIT 1) AS source_url
    """
    upcoming_status_ph = ",".join(["?"] * len(CONFIRMED_FAMILY_STATUSES))
    upcoming = conn.execute(
        f"""
        SELECT e.id, e.ticker, e.company_name, e.event_type, e.start_date,
               e.end_date, e.multi_day, e.status, e.confidence,
               {src_cols}
        FROM events e
        WHERE e.status IN ({upcoming_status_ph})
          AND e.event_type IN ({type_placeholders})
          AND e.start_date IS NOT NULL
          AND e.start_date >= ?
        ORDER BY e.start_date ASC, e.ticker ASC
        """,
        (*CONFIRMED_FAMILY_STATUSES, *pushable_types, today_iso),
    ).fetchall()

    ytd_status_ph = ",".join(["?"] * len(YTD_STATUSES))
    year_start = today_iso[:4] + "-01-01"
    ytd = conn.execute(
        f"""
        SELECT e.id, e.ticker, e.company_name, e.event_type, e.start_date,
               e.end_date, e.multi_day, e.status, e.confidence,
               {src_cols}
        FROM events e
        WHERE e.status IN ({ytd_status_ph})
          AND e.event_type IN ({type_placeholders})
          AND e.start_date IS NOT NULL
          AND e.start_date >= ?
          AND e.start_date < ?
        ORDER BY e.start_date DESC, e.ticker ASC
        """,
        (*YTD_STATUSES, *pushable_types, year_start, today_iso),
    ).fetchall()
    return upcoming, ytd


def _event_line(row) -> str:
    """One mrkdwn bullet for the Upcoming / YTD lists — carries the source link."""
    ticker = row["ticker"] or ""
    company = row["company_name"] or ""
    type_label = EVENT_TYPE_LABELS.get(row["event_type"], row["event_type"])
    when = row["start_date"] or "TBD"
    if row["multi_day"] and _row_get(row, "end_date"):
        when = f"{row['start_date']} – {row['end_date']}"
    line = f"• `{ticker}`"
    if company:
        line += f" {company}"
    line += f" — *{type_label}* · {when}"
    link = _source_link(row)
    if link:
        line += f" · {link}"
    return line


# Slack hard-caps a section's text at 3000 chars; stay well under and split a
# long list across multiple section blocks rather than truncating (silent-drop
# guard). Header rides in the first block only.
_SECTION_TEXT_CAP = 2800


def _mrkdwn_list_blocks(header: str, rows, empty: str) -> list[dict]:
    """Render a labeled list of events as one-or-more section/mrkdwn blocks."""
    if not rows:
        return [{"type": "section", "text": {"type": "mrkdwn",
                                             "text": f"{header}\n{empty}"}}]
    blocks: list[dict] = []
    buf = header
    for r in rows:
        ln = _event_line(r)
        candidate = f"{buf}\n{ln}"
        if len(candidate) > _SECTION_TEXT_CAP and buf:
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": buf}})
            buf = ln
        else:
            buf = candidate
    if buf:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
    return blocks


def build_monday_blocks(conn, today_iso: str) -> tuple[list[dict], int, int]:
    """Build the Monday-digest Block Kit payload. Pure — no network. Returns
    (blocks, count_in_30, count_upcoming) for callers/verification."""
    type_placeholders = ",".join(["?"] * len(PUSHABLE_EVENT_TYPES))
    pushable_types = sorted(PUSHABLE_EVENT_TYPES)
    in_30 = conn.execute(
        f"""
        SELECT e.id, e.ticker, e.event_type, e.start_date, e.end_date,
               e.multi_day, e.imprecise_hint, e.status, e.confidence,
               (SELECT s.source_type FROM event_sources s
                 WHERE s.event_id = e.id ORDER BY s.id ASC LIMIT 1) AS primary_source
        FROM events e
        WHERE e.status IN ('confirmed','reminded_30','reminded_7','day_of')
          AND e.event_type IN ({type_placeholders})
          AND e.start_date IS NOT NULL
          AND e.start_date >= ?
          AND e.start_date <= date(?, '+30 days')
        ORDER BY e.start_date ASC
        """,
        (*pushable_types, today_iso, today_iso),
    ).fetchall()

    in_7 = [r for r in in_30 if r["start_date"] <= _date_plus(today_iso, 7)]
    upcoming, ytd = query_upcoming_ytd(conn, today_iso)

    summary = (
        f":calendar: *Analyst Days — Monday Outlook* ({today_iso})  |  "
        f"*{len(in_30)}* in next 30d  ·  *{len(in_7)}* in next 7d"
    )

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Next 7 days ({len(in_7)})*\n"
                    + _grouped_table(in_7, today_iso)}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Next 30 days ({len(in_30)})*\n"
                    + _grouped_table(in_30, today_iso)}},
        {"type": "divider"},
    ]
    blocks += _mrkdwn_list_blocks(
        f"*Upcoming Analyst Days ({len(upcoming)})* — full forward calendar",
        upcoming,
        empty="_(none scheduled — run --discover)_",
    )
    blocks += _mrkdwn_list_blocks(
        f"*Analyst Days Year-to-Date ({len(ytd)})* — already held in "
        f"{today_iso[:4]}",
        ytd,
        empty="_(none held yet this year)_",
    )
    blocks.append({"type": "context", "elements": [{
        "type": "mrkdwn",
        "text": "_Upcoming = confirmed Investor / Analyst / R&D / Capital Markets "
                "Days dated today or later. Year-to-Date = the same, already held "
                "this calendar year. Links point to the announcement source._",
    }]})
    return blocks, len(in_30), len(upcoming)


def post_monday_digest(conn, today_iso: Optional[str] = None) -> int:
    today_iso = today_iso or date.today().isoformat()
    blocks, count_30, count_upcoming = build_monday_blocks(conn, today_iso)
    _post({
        "text": f"Analyst Days Monday outlook — {count_30} in 30d · "
                f"{count_upcoming} upcoming",
        "blocks": blocks,
    })
    return count_30


def _date_plus(iso: str, days: int) -> str:
    from datetime import date as _d, timedelta
    return (_d.fromisoformat(iso) + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Sanity ping
# ---------------------------------------------------------------------------


def post_test(message: str = "analyst-days bot online") -> None:
    _post({"text": f":white_check_mark: {message}"})
