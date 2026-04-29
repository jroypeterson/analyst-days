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
from datetime import date
from typing import Iterable, Optional

import requests

WEBHOOK_ENV = "SLACK_WEBHOOK_ANALYST_DAYS"

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
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    # Slack returns plain "ok" body on success
    if r.text.strip() != "ok":
        raise RuntimeError(f"Slack webhook returned: {r.text!r}")


# ---------------------------------------------------------------------------
# Per-event confirmation ping
# ---------------------------------------------------------------------------


def post_confirmed(event_row) -> None:
    """Per-event ping fired when status flips to confirmed.

    `event_row` is a sqlite3.Row (or any mapping) from the events table.
    """
    e = event_row
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
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "fields": fields},
    ]

    _post({
        "text": f"New {type_label} for {e['ticker']}",
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
    """All future events (confirmed + discovered + tentative) joined with primary source type."""
    return conn.execute(
        """
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
          AND (e.start_date IS NULL OR e.start_date >= ?)
        ORDER BY
          CASE WHEN e.start_date IS NULL THEN 1 ELSE 0 END,  -- precise dates first
          e.start_date ASC,
          e.ticker ASC
        """,
        (today_iso,),
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


def post_monday_digest(conn, today_iso: Optional[str] = None) -> int:
    today_iso = today_iso or date.today().isoformat()
    in_30 = conn.execute(
        """
        SELECT e.id, e.ticker, e.event_type, e.start_date, e.end_date,
               e.multi_day, e.imprecise_hint, e.status, e.confidence,
               (SELECT s.source_type FROM event_sources s
                 WHERE s.event_id = e.id ORDER BY s.id ASC LIMIT 1) AS primary_source
        FROM events e
        WHERE e.status IN ('confirmed','reminded_30','reminded_7','day_of')
          AND e.start_date IS NOT NULL
          AND e.start_date >= ?
          AND e.start_date <= date(?, '+30 days')
        ORDER BY e.start_date ASC
        """,
        (today_iso, today_iso),
    ).fetchall()

    in_7 = [r for r in in_30 if r["start_date"] <= _date_plus(today_iso, 7)]

    summary = (
        f":calendar: *Analyst Days — Monday Outlook* ({today_iso})  |  "
        f"*{len(in_30)}* in next 30d  ·  *{len(in_7)}* in next 7d"
    )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Next 7 days ({len(in_7)})*\n"
                    + _grouped_table(in_7, today_iso)}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Next 30 days ({len(in_30)})*\n"
                    + _grouped_table(in_30, today_iso)}},
    ]
    _post({
        "text": f"Analyst Days Monday outlook — {len(in_30)} in 30d / {len(in_7)} in 7d",
        "blocks": blocks,
    })
    return len(in_30)


def _date_plus(iso: str, days: int) -> str:
    from datetime import date as _d, timedelta
    return (_d.fromisoformat(iso) + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Sanity ping
# ---------------------------------------------------------------------------


def post_test(message: str = "analyst-days bot online") -> None:
    _post({"text": f":white_check_mark: {message}"})
