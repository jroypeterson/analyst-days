"""Digest rendering — the Monday "forward 30/7" email digest (HTML).

The Slack digests live in src/outputs/slack.py (Block Kit). This module
renders the same Monday "imminent" view as an HTML email body. Tickers are
rendered as <code> monospace chips to match the workspace backtick-chip
ticker convention (#67).

Query shape mirrors slack.post_monday_digest: confirmed (or already-reminded)
pushable events with a precise start_date, in the forward 30-day window, with
the forward 7-day subset broken out.
"""
from __future__ import annotations

import html
import sqlite3
from datetime import date, timedelta
from typing import Optional

from src.state.events_repo import PUSHABLE_EVENT_TYPES

EVENT_TYPE_LABELS = {
    "investor_day": "Investor Day",
    "analyst_day": "Analyst Day",
    "rd_day": "R&D Day",
    "capital_markets_day": "Capital Markets Day",
    "conference": "Conference",
}

SOURCE_LABELS = {
    "8K": "8-K",
    "IR_PAGE": "IR page",
    "PRESS_RELEASE": "Press release",
    "TAVILY_HIT": "Web",
    "MANUAL": "Manual",
}


def query_monday(
    conn: sqlite3.Connection, today_iso: str
) -> tuple[list, list]:
    """Return (in_30, in_7) lists of confirmed pushable upcoming events."""
    type_placeholders = ",".join(["?"] * len(PUSHABLE_EVENT_TYPES))
    pushable_types = sorted(PUSHABLE_EVENT_TYPES)
    in_30 = conn.execute(
        f"""
        SELECT e.id, e.ticker, e.company_name, e.event_type, e.start_date,
               e.end_date, e.multi_day, e.status, e.confidence,
               (SELECT s.source_type FROM event_sources s
                 WHERE s.event_id = e.id ORDER BY s.id ASC LIMIT 1) AS primary_source,
               (SELECT s.source_url FROM event_sources s
                 WHERE s.event_id = e.id ORDER BY s.id ASC LIMIT 1) AS source_url
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
    cutoff_7 = (date.fromisoformat(today_iso) + timedelta(days=7)).isoformat()
    in_7 = [r for r in in_30 if r["start_date"] <= cutoff_7]
    return in_30, in_7


def _ticker_chip(ticker: str) -> str:
    return f"<code>{html.escape(ticker or '')}</code>"


def _when(row) -> str:
    if row["multi_day"] and row["end_date"]:
        return f"{row['start_date']} &ndash; {row['end_date']}"
    return html.escape(row["start_date"] or "TBD")


def _rows_html(rows) -> str:
    if not rows:
        return '<tr><td colspan="5" style="padding:6px 10px;color:#888;">(none)</td></tr>'
    out = []
    for r in rows:
        type_label = EVENT_TYPE_LABELS.get(r["event_type"], r["event_type"])
        src = SOURCE_LABELS.get(r["primary_source"] or "", r["primary_source"] or "")
        company = html.escape(r["company_name"] or "")
        url = r["source_url"] or ""
        src_cell = f'<a href="{html.escape(url)}">{html.escape(src)}</a>' if url else html.escape(src)
        out.append(
            "<tr>"
            f'<td style="padding:6px 10px;white-space:nowrap;">{_when(r)}</td>'
            f'<td style="padding:6px 10px;">{_ticker_chip(r["ticker"])}'
            f'<span style="color:#666;"> {company}</span></td>'
            f'<td style="padding:6px 10px;">{html.escape(type_label)}</td>'
            f'<td style="padding:6px 10px;text-align:right;">{(r["confidence"] or 0.0):.2f}</td>'
            f'<td style="padding:6px 10px;">{src_cell}</td>'
            "</tr>"
        )
    return "\n".join(out)


def _table(title: str, rows) -> str:
    header = (
        '<tr style="background:#f0f0f0;text-align:left;">'
        '<th style="padding:6px 10px;">Date</th>'
        '<th style="padding:6px 10px;">Ticker</th>'
        '<th style="padding:6px 10px;">Type</th>'
        '<th style="padding:6px 10px;text-align:right;">Conf.</th>'
        '<th style="padding:6px 10px;">Source</th></tr>'
    )
    return (
        f'<h3 style="font-family:sans-serif;margin:18px 0 6px;">{html.escape(title)}</h3>'
        '<table style="border-collapse:collapse;font-family:sans-serif;'
        'font-size:14px;border:1px solid #ddd;">'
        f"{header}\n{_rows_html(rows)}</table>"
    )


def render_monday_html(
    conn: sqlite3.Connection, today_iso: Optional[str] = None
) -> tuple[str, str, int]:
    """Render the Monday digest email. Returns (subject, html_body, count_30)."""
    today_iso = today_iso or date.today().isoformat()
    in_30, in_7 = query_monday(conn, today_iso)

    subject = (
        f"[ClaudeFin] analyst-days — Monday Outlook ({today_iso}): "
        f"{len(in_30)} in 30d / {len(in_7)} in 7d"
    )
    body = (
        '<div style="font-family:sans-serif;color:#222;">'
        f'<h2 style="margin:0 0 4px;">\U0001f4c5 Analyst Days &mdash; Monday Outlook</h2>'
        f'<p style="color:#666;margin:0 0 8px;">{today_iso} &middot; '
        f"<b>{len(in_30)}</b> in next 30 days &middot; <b>{len(in_7)}</b> in next 7 days</p>"
        f"{_table('Next 7 days', in_7)}"
        f"{_table('Next 30 days', in_30)}"
        '<p style="color:#999;font-size:12px;margin-top:18px;">'
        "Confirmed Investor / Analyst / R&amp;D / Capital Markets Days on the core "
        "watchlist. Sent by analyst-days automation.</p>"
        "</div>"
    )
    return subject, body, len(in_30)
