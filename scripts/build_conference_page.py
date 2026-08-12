"""Render the conference calendar to a self-contained HTML page.

    python scripts/build_conference_page.py [--years 2025 2026] [--out PATH]

The page is published as a Claude Artifact. Regenerate and re-publish to the SAME
artifact URL whenever the calendar changes — this script exists so the page is a
projection of `data/events.db`, never a hand-maintained copy that drifts from it.

Self-contained by requirement: the artifact CSP blocks every external host, so all
CSS is inline and no font or script is fetched.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DB_PATH = os.path.join(REPO, "data", "events.db")
DEFAULT_OUT = os.path.join(REPO, "exports", "conference_calendar.html")

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _esc(s: str | None) -> str:
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _why(notes: str | None) -> str:
    """Strip the `[verified YYYY-MM-DD]` provenance prefix off the notes blob."""
    n = (notes or "").strip()
    if n.startswith("["):
        close = n.find("]")
        if close != -1:
            return n[close + 1:].strip()
    return n


def _verified_on(notes: str | None) -> str:
    n = (notes or "").strip()
    if n.startswith("[") and "]" in n:
        inner = n[1:n.find("]")].split()
        return inner[1] if len(inner) > 1 else ""
    return ""


def _span(start: str, end: str | None) -> str:
    """'Jan 13-16' / 'Nov 30 - Dec 4' — the year lives in the section header."""
    sy, sm, sd = start.split("-")
    if not end:
        return f"{MONTHS[int(sm) - 1]} {int(sd)}"
    ey, em, ed = end.split("-")
    if sm == em:
        return f"{MONTHS[int(sm) - 1]} {int(sd)}-{int(ed)}"
    return f"{MONTHS[int(sm) - 1]} {int(sd)} - {MONTHS[int(em) - 1]} {int(ed)}"


def load(db_path: str, years: list[str]):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name, short_name, series, sector, host_ticker, start_date, "
        "end_date, location, url, notes FROM conferences ORDER BY start_date ASC"
    ).fetchall()
    conn.close()
    return [r for r in rows if r["start_date"][:4] in years]


def build(rows, years: list[str], today_iso: str) -> str:
    by_year: dict[str, list] = {y: [] for y in years}
    for r in rows:
        by_year[r["start_date"][:4]].append(r)

    held = sum(1 for r in rows if r["start_date"] < today_iso)
    upcoming = len(rows) - held

    # Undated circuit entries — imported so the page states what it is NOT showing
    # rather than silently presenting itself as the whole circuit.
    from src.seed_conferences import unconfirmed
    undated = unconfirmed()

    sections = []
    # Newest year first: "this year" is the one being planned around.
    for y in sorted(years, reverse=True):
        yr_rows = by_year.get(y, [])
        if not yr_rows:
            continue
        y_held = sum(1 for r in yr_rows if r["start_date"] < today_iso)
        items = []
        marker_done = False
        for r in yr_rows:
            is_past = r["start_date"] < today_iso
            # The "we are here" rule drops in once, before the first future row of
            # the current year. It is the only reason the year reads as a position
            # rather than a list.
            if not is_past and not marker_done and y == today_iso[:4]:
                items.append(
                    '<li class="now" aria-label="Today">'
                    f'<span class="now-date">{_esc(_span(today_iso, None))}</span>'
                    '<span class="now-rule"></span>'
                    '<span class="now-label">today</span></li>'
                )
                marker_done = True
            v = _verified_on(r["notes"])
            items.append(
                '<li class="row{cls}">'
                '<span class="when">{when}</span>'
                '<span class="what">'
                '<a class="name" href="{url}" target="_blank" rel="noopener">{short}</a>'
                '<span class="full">{full}</span>'
                '<span class="why">{why}</span>'
                '</span>'
                '<span class="meta">'
                '<span class="place">{place}</span>'
                '<span class="chip {chipcls}">{chip}</span>'
                '{host}'
                '</span>'
                '</li>'.format(
                    cls=" is-past" if is_past else "",
                    when=_esc(_span(r["start_date"], r["end_date"])),
                    url=_esc(r["url"] or "#"),
                    short=_esc(r["short_name"] or r["name"]),
                    full=_esc(r["name"]),
                    why=_esc(_why(r["notes"])),
                    place=_esc(r["location"] or "—"),
                    chipcls="past" if is_past else "next",
                    chip="held" if is_past else "upcoming",
                    host=(f'<span class="chip host">{_esc(r["host_ticker"])}</span>'
                          if r["host_ticker"] else ""),
                )
            )
        sections.append(
            f'<section class="year" aria-labelledby="y{y}">\n'
            f'<div class="year-head">'
            f'<h2 id="y{y}">{y}</h2>'
            f'<p class="year-count">{len(yr_rows)} meetings · '
            f'{y_held} held · {len(yr_rows) - y_held} to come</p>'
            f'</div>\n<ol class="rows">\n' + "\n".join(items) + "\n</ol>\n</section>"
        )

    undated_html = ""
    if undated:
        chips = "".join(
            f'<li><span class="u-name">{_esc(c.short_name)}</span>'
            f'<span class="u-why">{_esc(c.why_it_matters.replace(" CONFIRM DATE.", "").replace(" CONFIRM DATE before relying on it.", ""))}</span></li>'
            for c in undated
        )
        undated_html = (
            '<section class="undated" aria-labelledby="undated-h">\n'
            '<h2 id="undated-h">On the circuit, not on the calendar</h2>\n'
            f'<p class="lede">{len(undated)} meetings are tracked but carry no confirmed '
            'date, so they are deliberately absent above. A wrong conference date '
            'silently mis-anchors every catalyst hung off it, so nothing here is '
            'guessed — a date is either verified against a source or the meeting waits.</p>\n'
            f'<ul class="ulist">{chips}</ul>\n</section>'
        )

    return PAGE.format(
        sections="\n".join(sections),
        undated=undated_html,
        total=len(rows),
        held=held,
        upcoming=upcoming,
        span=" & ".join(sorted(years)),
        generated=today_iso,
    )


PAGE = """<title>Healthcare Conference Circuit</title>
<style>
  /* Light is the base set. Every colour below is a token so the dark variants
     can redefine tokens only — no component rule lives inside a media query. */
  :root {{
    --ground:      #F4F7F5;
    --surface:     #FFFFFF;
    --surface-2:   #EDF2EF;
    --ink:         #0F1618;
    --ink-2:       #3D4B4C;
    --ink-3:       #647472;
    --rule:        #D9E2DE;
    --rule-2:      #E9EFEC;
    --accent:      #0B6A56;
    --accent-ink:  #0B6A56;
    --accent-soft: #DEEDE7;
    --amber:       #855312;
    --amber-soft:  #F3E8D6;
    --shadow: 0 1px 2px rgba(15,22,24,.05), 0 8px 24px -12px rgba(15,22,24,.18);
    --display: Georgia, "Iowan Old Style", "Palatino Linotype", Palatino, serif;
    --body: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --mono: ui-monospace, "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --ground:      #0B1113;
      --surface:     #121A1C;
      --surface-2:   #162023;
      --ink:         #E4ECE9;
      --ink-2:       #B3C3BF;
      --ink-3:       #7E918D;
      --rule:        #223032;
      --rule-2:      #1A2527;
      --accent:      #52C0A0;
      --accent-ink:  #7FD3B9;
      --accent-soft: #10302A;
      --amber:       #D9A860;
      --amber-soft:  #2E2413;
      --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.7);
    }}
  }}
  :root[data-theme="dark"] {{
    --ground:      #0B1113;
    --surface:     #121A1C;
    --surface-2:   #162023;
    --ink:         #E4ECE9;
    --ink-2:       #B3C3BF;
    --ink-3:       #7E918D;
    --rule:        #223032;
    --rule-2:      #1A2527;
    --accent:      #52C0A0;
    --accent-ink:  #7FD3B9;
    --accent-soft: #10302A;
    --amber:       #D9A860;
    --amber-soft:  #2E2413;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.7);
  }}

  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--ground);
    color: var(--ink);
    font-family: var(--body);
    font-size: 16px;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{
    max-width: 60rem;
    margin: 0 auto;
    padding: clamp(2rem, 5vw, 4.5rem) clamp(1rem, 4vw, 2.5rem) 5rem;
    display: flex;
    flex-direction: column;
    gap: clamp(2.5rem, 5vw, 4rem);
  }}

  /* ---- masthead ---- */
  header {{ display: flex; flex-direction: column; gap: .9rem; }}
  .eyebrow {{
    font-family: var(--mono);
    font-size: .72rem;
    letter-spacing: .13em;
    text-transform: uppercase;
    color: var(--ink-3);
  }}
  h1 {{
    font-family: var(--display);
    font-weight: 400;
    font-size: clamp(2.1rem, 5.5vw, 3.3rem);
    line-height: 1.08;
    letter-spacing: -.015em;
    margin: 0;
    text-wrap: balance;
  }}
  h1 em {{ font-style: italic; color: var(--accent-ink); }}
  .lede {{
    margin: 0;
    max-width: 62ch;
    color: var(--ink-2);
    font-size: 1.02rem;
  }}
  .tally {{
    display: flex;
    flex-wrap: wrap;
    gap: 0 1.6rem;
    margin-top: .4rem;
    padding-top: 1rem;
    border-top: 1px solid var(--rule);
    font-family: var(--mono);
    font-size: .8rem;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
  }}
  .tally b {{ color: var(--ink); font-weight: 600; }}

  /* ---- year sections ---- */
  .year-head {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 1.1rem;
  }}
  .year-head h2 {{
    font-family: var(--display);
    font-weight: 400;
    font-size: clamp(1.6rem, 3.5vw, 2.1rem);
    margin: 0;
    letter-spacing: -.01em;
  }}
  .year-count {{
    margin: 0;
    font-family: var(--mono);
    font-size: .76rem;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
  }}

  ol.rows {{ list-style: none; margin: 0; padding: 0;
             display: flex; flex-direction: column; }}
  li.row {{
    display: grid;
    grid-template-columns: 8.5rem minmax(0, 1fr) auto;
    gap: 0 1.4rem;
    align-items: baseline;
    padding: 1rem 1rem 1rem .9rem;
    border-bottom: 1px solid var(--rule-2);
    background: var(--surface);
  }}
  li.row:first-of-type {{ border-top-left-radius: 6px; border-top-right-radius: 6px; }}
  li.row:last-child {{ border-bottom: 0;
                       border-bottom-left-radius: 6px; border-bottom-right-radius: 6px; }}
  ol.rows {{ box-shadow: var(--shadow); border-radius: 6px; overflow: hidden; }}
  li.row.is-past {{ background: var(--surface-2); }}

  .when {{
    font-family: var(--mono);
    font-size: .82rem;
    font-variant-numeric: tabular-nums;
    color: var(--ink-2);
    letter-spacing: -.01em;
    white-space: nowrap;
  }}
  li.row:not(.is-past) .when {{ color: var(--accent-ink); font-weight: 600; }}

  .what {{ display: flex; flex-direction: column; gap: .15rem; min-width: 0; }}
  a.name {{
    font-weight: 600;
    font-size: 1rem;
    color: var(--ink);
    text-decoration: none;
    border-bottom: 1px solid transparent;
    align-self: flex-start;
  }}
  a.name:hover, a.name:focus-visible {{
    color: var(--accent-ink);
    border-bottom-color: var(--accent);
  }}
  a.name:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 3px; }}
  .full {{ font-size: .82rem; color: var(--ink-3); }}
  .why {{ font-size: .86rem; color: var(--ink-2); margin-top: .3rem; max-width: 58ch; }}

  .meta {{ display: flex; align-items: center; gap: .5rem; flex-wrap: wrap;
           justify-content: flex-end; }}
  .place {{ font-size: .82rem; color: var(--ink-3); }}
  .chip {{
    font-family: var(--mono);
    font-size: .64rem;
    letter-spacing: .09em;
    text-transform: uppercase;
    padding: .2rem .45rem;
    border-radius: 3px;
    white-space: nowrap;
  }}
  .chip.next {{ background: var(--accent-soft); color: var(--accent-ink); }}
  .chip.past {{ background: transparent; color: var(--ink-3);
                border: 1px solid var(--rule); }}
  .chip.host {{ background: var(--amber-soft); color: var(--amber); }}

  /* the "we are here" rule */
  li.now {{
    display: flex; align-items: center; gap: .8rem;
    padding: .55rem 1rem .55rem .9rem;
    background: var(--surface);
    border-bottom: 1px solid var(--rule-2);
  }}
  .now-date {{
    font-family: var(--mono); font-size: .74rem; color: var(--accent-ink);
    font-variant-numeric: tabular-nums; width: 7.6rem; flex: none;
  }}
  .now-rule {{ flex: 1; height: 1px; background: var(--accent); opacity: .55; }}
  .now-label {{
    font-family: var(--mono); font-size: .64rem; letter-spacing: .13em;
    text-transform: uppercase; color: var(--accent-ink);
  }}

  /* ---- undated ---- */
  .undated h2 {{
    font-family: var(--display); font-weight: 400;
    font-size: clamp(1.3rem, 3vw, 1.7rem); margin: 0 0 .6rem;
  }}
  ul.ulist {{
    list-style: none; margin: 1.1rem 0 0; padding: 0;
    display: grid; gap: .1rem;
    grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
    border-radius: 6px; overflow: hidden; box-shadow: var(--shadow);
  }}
  ul.ulist li {{
    background: var(--surface); padding: .8rem 1rem;
    display: flex; flex-direction: column; gap: .15rem;
  }}
  .u-name {{ font-family: var(--mono); font-size: .82rem; font-weight: 600;
             color: var(--amber); }}
  .u-why {{ font-size: .82rem; color: var(--ink-2); }}

  footer {{
    border-top: 1px solid var(--rule);
    padding-top: 1.2rem;
    font-size: .8rem;
    color: var(--ink-3);
    display: flex; flex-direction: column; gap: .35rem;
  }}
  footer code {{ font-family: var(--mono); font-size: .76rem; color: var(--ink-2); }}

  /* Collapse to one column early. The three-column row needs real width for the
     date, the name and the location before the middle column starts squeezing,
     and the artifact viewer can be rendered narrower than a full window. */
  @media (max-width: 52rem) {{
    li.row {{ grid-template-columns: 1fr; gap: .35rem; }}
    .meta {{ justify-content: flex-start; }}
    .now-date {{ width: auto; }}
  }}
</style>

<div class="wrap">
  <header>
    <p class="eyebrow">Analyst Days · conference calendar</p>
    <h1>The meetings that move <em>healthcare</em></h1>
    <p class="lede">Every conference on the tracked circuit for {span} — the
      scientific congresses where trial data lands and the investor conferences
      where guidance resets. Conference-anchored: these are the meetings
      themselves, not our companies' appearances at them.</p>
    <p class="tally">
      <span><b>{total}</b> meetings</span>
      <span><b>{held}</b> held</span>
      <span><b>{upcoming}</b> upcoming</span>
      <span>every date web-verified</span>
    </p>
  </header>

  {sections}

  {undated}

  <footer>
    <span>Curated in <code>analyst-days/src/seed_conferences.py</code> — that
      constant is the source of truth; the database is a projection of it.</span>
    <span>Generated {generated} from <code>data/events.db</code> via
      <code>scripts/build_conference_page.py</code>.</span>
  </footer>
</div>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--years", nargs="+", default=None,
                    help="Default: last year and this year.")
    ap.add_argument("--today", default=None, help="Override 'today' (testing).")
    args = ap.parse_args(argv)

    today = args.today or date.today().isoformat()
    years = args.years or [str(int(today[:4]) - 1), today[:4]]

    rows = load(args.db, years)
    if not rows:
        print(f"No conferences found for {years} in {args.db}")
        return 1
    html = build(rows, years, today)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print(f"{len(rows)} conferences ({', '.join(years)}) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
