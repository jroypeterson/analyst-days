"""The two conference channels: weekly look-ahead, pinned overview, bookmark.

    #conferences-industry   medical-specialty meetings and the operator ("Main Street")
                            meetings: nursing facilities, labs, pharmacies, hospitals
    #conferences-wall-st    bank / sell-side healthcare investor conferences, with the
                            companies from JP's lists that have said they will present

Which channel a conference belongs to is `seed_conferences.channel_for` (by `kind`).

WHO IS PRESENTING comes from this repo's own discovery, not from the banks' attendee
lists (gated, board #58). `events.event_type='conference'` rows are company-anchored
("WST will present at the BofA conference on May 12") and carry no conference name, but
the stored source excerpt does. A company is listed under a conference when its
conference event falls inside the conference's dates AND an excerpt names the
conference (`SeriesMeta.match`). Date overlap alone is not enough: Stifel, UBS and
Jefferies London share one week in November.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from typing import Optional

from src import seed_conferences as sc
from src.conference_briefs import fmt_dates
from src.outputs import slack_bot

INVENTORY_URL = "https://claude.ai/artifact/SEkWwJ9WceuymgtfQdCiBF"
INVENTORY_TITLE = "Conference inventory (all meetings)"
PIN_MARKER = ":pushpin: *About this channel*"

DETAIL_WEEKS = {"industry": 6, "wallst": 8}
LATER_LINES = 10
CHANNEL_ENV = {"industry": slack_bot.INDUSTRY_CHANNEL_ENV,
               "wallst": slack_bot.WALLST_CHANNEL_ENV}
CHANNEL_NAME = {"industry": "#conferences-industry", "wallst": "#conferences-wall-st"}

_DEAD_STATUSES = ("cancelled", "superseded")


def upcoming(channel: str, today: date) -> list[sc.Conference]:
    """Instances in this channel that have not ended, soonest first."""
    rows = [c for c in sc.CONFERENCES
            if c.start and c.end and sc.channel_for(c) == channel
            and date.fromisoformat(c.end) >= today]
    return sorted(rows, key=lambda c: (c.start, c.short_name))


def undated_series(channel: str, today: date) -> list[str]:
    """Series tracked for this channel with NO instance still ahead: the date is not
    published yet, or nobody has rolled the series forward. Named, never guessed."""
    ahead = {c.series for c in upcoming(channel, today)}
    kinds = {}
    for c in sc.CONFERENCES:
        kinds.setdefault(c.series, sc.channel_for(c))
    out = []
    for slug_, m in sc.SERIES_META.items():
        ch = kinds.get(slug_) or ("wallst" if m.category == "Bank conferences" else "industry")
        if ch == channel and slug_ not in ahead:
            out.append(m.full_name)
    return sorted(out)


def presenters(conn, conf: sc.Conference) -> list[str]:
    """Tickers from the scan set that have announced a slot at this conference."""
    phrases = sc.meta_for(conf).match
    if conn is None or not phrases:
        return []
    # Whole words only: a bare substring would find "Citi" inside "Twin Cities".
    named = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in phrases) + r")\b", re.I)
    rows = conn.execute(
        """
        SELECT e.ticker, COALESCE(s.source_excerpt, '')
        FROM events e JOIN event_sources s ON s.event_id = e.id
        WHERE e.event_type = 'conference'
          AND e.start_date BETWEEN ? AND ?
          AND COALESCE(e.status, '') NOT IN (?, ?)
        """,
        (conf.start, conf.end, *_DEAD_STATUSES),
    ).fetchall()
    found = {r[0] for r in rows if named.search(r[1])}
    return sorted(found)


def _entry(conf: sc.Conference, conn, channel: str) -> list[str]:
    m = sc.meta_for(conf)
    where = f" · {conf.location}" if conf.location else ""
    out = [f"*{conf.short_name}* · {m.full_name}",
           f"> *When*  {fmt_dates(conf.start, conf.end)}{where} · <{conf.url}|conference site>"]
    if channel == "industry":
        out.append(f"> *For*  {m.audience}")
    out.append(f"> *Standing*  {m.standing}")
    if m.attendance:
        out.append(f"> *Size*  {m.attendance}")
    if channel == "wallst":
        names = presenters(conn, conf)
        if names:
            out.append(f"> *Presenting, from your lists ({len(names)})*  "
                       + " ".join(f"`{t}`" for t in names))
        else:
            out.append("> *Presenting, from your lists*  none announced yet")
    return out


def build_weekly(channel: str, today: date, conn=None) -> tuple[str, int]:
    """The Monday look-ahead for one channel. Returns (mrkdwn text, upcoming count)."""
    rows = upcoming(channel, today)
    horizon = today + timedelta(weeks=DETAIL_WEEKS[channel])
    near = [c for c in rows if date.fromisoformat(c.start) <= horizon]
    later = [c for c in rows if date.fromisoformat(c.start) > horizon]
    what = ("Bank healthcare conferences" if channel == "wallst"
            else "Medical and industry conferences")
    L = [f":calendar: *{what}* · next {DETAIL_WEEKS[channel]} weeks · as of "
         f"{today.isoformat()}"]
    if not near:
        L.append(f"_Nothing on the list starts in the next {DETAIL_WEEKS[channel]} weeks._")
    for c in near:
        L += ["", *_entry(c, conn, channel)]
    if later:
        shown = later[:LATER_LINES]
        L += ["", f"*Later ({len(later)})*"]
        L += [f"• {fmt_dates(c.start, c.end)} · {c.short_name} · "
              f"{sc.meta_for(c).audience if channel == 'industry' else sc.meta_for(c).organizer}"
              for c in shown]
        if len(later) > len(shown):   # counted and pointed at, never silently cut
            L.append(f"• and {len(later) - len(shown)} more after "
                     f"{fmt_dates(shown[-1].start, shown[-1].end)}, in the inventory")
    undated = undated_series(channel, today)
    if undated:
        L += ["", f"*Next date not published yet ({len(undated)})*",
              "> " + " · ".join(undated)]
    if channel == "wallst":
        L += ["", "_Presenters are the companies on your lists whose own press release or "
                  "filing names the conference. It is not the bank's attendee list._"]
    L.append(f"<{INVENTORY_URL}|Full conference inventory>")
    return "\n".join(L), len(rows)


def overview_text(channel: str) -> str:
    """The pinned card. Bullets, per JP 2026-11-17: *"For a pin I prefer bullet format
    since easier to read"* (SLACK_STYLE.md section 6)."""
    n_series = len({c.series for c in sc.CONFERENCES if sc.channel_for(c) == channel})
    if channel == "industry":
        body = [
            "*What posts here*",
            "• *Pre-conference alert* — up to 7 days before a medical meeting, only when "
            "something significant is expected (a pivotal readout, first full data)",
            "• *Post-conference summary* — 2 to 21 days after a meeting ends: at most 4 "
            "summary bullets, then each finding with its numbers, then links",
            "• *Monday look-ahead* — what starts in the next 6 weeks",
            "*Every post says*",
            "• What the name stands for and which specialty or operator group attends",
            "• Whether it is the main meeting for that group or one of several",
            "• Attendance, where the organizer publishes it",
            "*What is covered*",
            f"• {n_series} meeting series: medical specialties (oncology, cardiology, "
            "kidney, OB-GYN, neurology and so on)",
            "• Operator meetings: nursing facilities and senior living, labs, pharmacies, "
            "hospitals and physician practices, payers",
            "*What is not here*",
            "• Bank investor conferences — those are in #conferences-wall-st",
            "• Company analyst and investor days — those are in #analyst-days",
            "*Caveats*",
            "• Summaries are researched from the web by a model, not from the primary "
            "papers. Check a figure before acting on it",
            "• New vs. dropped topics compare against the prior year's stored summary; "
            "the first year of a series has a weaker baseline",
        ]
    else:
        body = [
            "*What posts here*",
            "• *Monday look-ahead* — bank healthcare conferences in the next 8 weeks, with "
            "the companies from your lists that have said they will present",
            "• *Post-conference summary* — for the J.P. Morgan Healthcare Conference only",
            "*Where the presenter list comes from*",
            "• Each company's own press release or SEC filing naming the conference",
            "• It is not the bank's attendee list (those are gated) and it only covers "
            "names on your Coverage Manager lists",
            "• A company appears once its announcement is picked up by the Monday scan",
            "*What is covered*",
            f"• {n_series} bank conference series (J.P. Morgan, Goldman Sachs, Morgan Stanley, "
            "TD Cowen, Jefferies, Piper Sandler and so on)",
            "*What is not here*",
            "• Medical and operator meetings — those are in #conferences-industry",
            "• Company analyst and investor days — those are in #analyst-days",
            "*Caveats*",
            "• Banks publish dates late. A series with no published date is listed as "
            "'not published yet', never guessed",
        ]
    return "\n".join([PIN_MARKER + f" · {CHANNEL_NAME[channel]}", *body,
                      f"<{INVENTORY_URL}|Full conference inventory> · run by "
                      "`analyst-days` (GitHub Actions, Mondays and Thursdays)"])


# ----------------------------------------------------------------- pin + bookmark


def refresh_pin(channel: str) -> str:
    """Post the overview, pin it, and remove this bot's earlier overview pins."""
    env = CHANNEL_ENV[channel]
    chan = os.environ[env].strip()
    me = slack_bot.call("auth.test", {})["user_id"]
    # Posting to a public channel needs no membership (chat:write.public); pinning does.
    slack_bot.call("conversations.join", {"channel": chan})
    old = [it["message"] for it in slack_bot.call("pins.list", {"channel": chan}).get("items", [])
           if it.get("message", {}).get("user") == me
           and PIN_MARKER in it["message"].get("text", "")]
    text = overview_text(channel)
    ts = slack_bot.post(env, text, f"About {CHANNEL_NAME[channel]}")
    slack_bot.call("pins.add", {"channel": chan, "timestamp": ts})
    for msg in old:   # only ever this bot's own earlier overview card
        slack_bot.call("pins.remove", {"channel": chan, "timestamp": msg["ts"]})
        slack_bot.call("chat.delete", {"channel": chan, "ts": msg["ts"]})
    return ts


def refresh_bookmark(channel: str) -> str:
    """Bookmark the inventory page; edit in place if the title already exists."""
    chan = os.environ[CHANNEL_ENV[channel]].strip()
    marks = slack_bot.call("bookmarks.list", {"channel_id": chan}).get("bookmarks", [])
    for b in marks:
        if b.get("link") == INVENTORY_URL or b.get("title") in (INVENTORY_TITLE,
                                                               "Conference Calendar"):
            if b.get("link") == INVENTORY_URL and b.get("title") == INVENTORY_TITLE:
                return "unchanged"
            slack_bot.call("bookmarks.edit", {"channel_id": chan, "bookmark_id": b["id"],
                                              "title": INVENTORY_TITLE, "link": INVENTORY_URL})
            return "updated"
    slack_bot.call("bookmarks.add", {"channel_id": chan, "title": INVENTORY_TITLE,
                                     "type": "link", "link": INVENTORY_URL,
                                     "emoji": ":spiral_calendar_pad:"})
    return "added"
