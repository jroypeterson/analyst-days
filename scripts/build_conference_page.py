"""Render the conference calendar to a self-contained HTML page.

    python scripts/build_conference_page.py [--years 2025 2026] [--out PATH]

The page is published as a Claude Artifact. Regenerate and re-publish to the SAME
artifact URL whenever the calendar changes — this script exists so the page is a
projection of `data/events.db`, never a hand-maintained copy that drifts from it.

Self-contained by requirement: the artifact CSP blocks every external host, so all
CSS and JS are inline and no font or script is fetched.
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

QUARTER_MONTHS = {1: "Jan - Mar", 2: "Apr - Jun", 3: "Jul - Sep", 4: "Oct - Dec"}


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


def _span(start: str, end: str | None) -> str:
    """'Jan 13-16' / 'Nov 30 - Dec 4' — the year lives in the section header."""
    _sy, sm, sd = start.split("-")
    if not end:
        return f"{MONTHS[int(sm) - 1]} {int(sd)}"
    _ey, em, ed = end.split("-")
    if sm == em:
        return f"{MONTHS[int(sm) - 1]} {int(sd)}-{int(ed)}"
    return f"{MONTHS[int(sm) - 1]} {int(sd)} - {MONTHS[int(em) - 1]} {int(ed)}"


def _quarter(iso: str) -> int:
    return (int(iso[5:7]) - 1) // 3 + 1


def _slug(s: str | None) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in (s or "none"))


def load(db_path: str, years: list[str]):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name, short_name, series, sector, kind, host_ticker, start_date, "
        "end_date, location, url, notes FROM conferences ORDER BY start_date ASC"
    ).fetchall()
    conn.close()
    return [r for r in rows if r["start_date"][:4] in years]


def build(rows, years: list[str], today_iso: str) -> str:
    """Quarterly grid: four boxes per year, each listing `Name (dates)`.

    Compact by construction — one line per meeting and no prose inside the grid.
    The detail (full name, venue, why it matters) rides on each row's `title`, so
    it stays available on hover without costing vertical space.
    """
    sectors = sorted({(r["sector"] or "Unclassified") for r in rows})
    kinds = sorted({(r["kind"] or "Unclassified") for r in rows})
    today_q, today_y = _quarter(today_iso), today_iso[:4]

    year_blocks = []
    for y in sorted(years, reverse=True):
        yr = [r for r in rows if r["start_date"][:4] == y]
        if not yr:
            continue
        cards = []
        for q in (1, 2, 3, 4):
            in_q = [r for r in yr if _quarter(r["start_date"]) == q]
            items = []
            for r in in_q:
                past = r["start_date"] < today_iso
                tip = r["name"]
                if r["location"]:
                    tip += " - " + r["location"]
                tip += " - " + _why(r["notes"])
                host = ""
                if r["host_ticker"]:
                    host = '<span class="h">' + _esc(r["host_ticker"]) + "</span>"
                items.append(
                    '<li class="item{cls}" data-sector="{sec}" data-kind="{kind}" '
                    'title="{tip}">'
                    '<a href="{url}" target="_blank" rel="noopener">{short}</a>'
                    '<span class="d">({when})</span>{host}</li>'.format(
                        cls=" gone" if past else "",
                        sec=_slug(r["sector"] or "Unclassified"),
                        kind=_slug(r["kind"] or "Unclassified"),
                        tip=_esc(tip),
                        url=_esc(r["url"] or "#"),
                        short=_esc((r["short_name"] or r["name"]).replace(" " + y, "")),
                        when=_esc(_span(r["start_date"], r["end_date"])),
                        host=host,
                    )
                )
            is_now = (y == today_y and q == today_q)
            cards.append(
                '<article class="q{now}">'
                "<header><h3>Q{q}</h3><span class=\"qm\">{months}</span>"
                '<span class="qn" data-count>{n}</span></header>'
                '<ul class="items">{items}</ul>'
                '<p class="empty"{hide}>none</p>'
                "</article>".format(
                    now=" is-now" if is_now else "",
                    q=q, months=QUARTER_MONTHS[q], n=len(in_q),
                    items="".join(items),
                    hide="" if not in_q else " hidden",
                )
            )
        held = sum(1 for r in yr if r["start_date"] < today_iso)
        year_blocks.append(
            '<section class="year">'
            '<div class="yh"><h2>{y}</h2>'
            '<span class="ym"><span data-year-count>{n}</span> meetings, {held} held</span>'
            '</div><div class="grid">{cards}</div></section>'.format(
                y=y, n=len(yr), held=held, cards="".join(cards))
        )

    def chips(group, values):
        out = ['<button class="chip on" data-group="' + group + '" data-value="all" '
               'aria-pressed="true">All</button>']
        for v in values:
            label = v.replace(" meeting", "").replace(" conference", "")
            out.append(
                '<button class="chip" data-group="' + group + '" data-value="'
                + _slug(v) + '" aria-pressed="false">' + _esc(label) + "</button>")
        return "".join(out)

    held_total = sum(1 for r in rows if r["start_date"] < today_iso)
    return (PAGE
            .replace("%%YEARS%%", "".join(year_blocks))
            .replace("%%SECTOR_CHIPS%%", chips("sector", sectors))
            .replace("%%KIND_CHIPS%%", chips("kind", kinds))
            .replace("%%TOTAL%%", str(len(rows)))
            .replace("%%HELD%%", str(held_total))
            .replace("%%SPAN%%", " & ".join(sorted(years)))
            .replace("%%GENERATED%%", today_iso))


PAGE = """<title>Healthcare Conference Circuit</title>
<style>
  /* Light is the base set. Every colour is a token, so the two dark variants
     redefine tokens only and no component rule lives inside a media query. */
  :root {
    --ground:#F4F7F5; --surface:#FFFFFF; --surface-2:#EBF1EE;
    --ink:#0F1618; --ink-2:#3D4B4C; --ink-3:#6B7B79;
    --rule:#D7E1DD; --rule-2:#E8EEEB;
    --accent:#0B6A56; --accent-ink:#0B6A56; --accent-soft:#DCEBE5;
    --amber:#855312; --amber-soft:#F3E8D6;
    --shadow:0 1px 2px rgba(15,22,24,.05), 0 6px 18px -12px rgba(15,22,24,.22);
    --display:Georgia,"Iowan Old Style","Palatino Linotype",Palatino,serif;
    --body:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --ground:#0B1113; --surface:#121A1C; --surface-2:#0F1719;
      --ink:#E4ECE9; --ink-2:#B3C3BF; --ink-3:#849692;
      --rule:#223032; --rule-2:#1A2527;
      --accent:#52C0A0; --accent-ink:#7FD3B9; --accent-soft:#12332C;
      --amber:#D9A860; --amber-soft:#2E2413;
      --shadow:0 1px 2px rgba(0,0,0,.4), 0 6px 18px -12px rgba(0,0,0,.8);
    }
  }
  :root[data-theme="dark"] {
    --ground:#0B1113; --surface:#121A1C; --surface-2:#0F1719;
    --ink:#E4ECE9; --ink-2:#B3C3BF; --ink-3:#849692;
    --rule:#223032; --rule-2:#1A2527;
    --accent:#52C0A0; --accent-ink:#7FD3B9; --accent-soft:#12332C;
    --amber:#D9A860; --amber-soft:#2E2413;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 6px 18px -12px rgba(0,0,0,.8);
  }

  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--ground); color:var(--ink);
    font-family:var(--body); font-size:15px; line-height:1.45;
    -webkit-font-smoothing:antialiased;
  }
  .wrap {
    max-width:72rem; margin:0 auto;
    padding:clamp(1.5rem,3vw,2.5rem) clamp(.9rem,2.5vw,1.75rem) 3rem;
    display:flex; flex-direction:column; gap:1.5rem;
  }

  header.top { display:flex; flex-direction:column; gap:.35rem; }
  .eyebrow {
    font-family:var(--mono); font-size:.66rem; letter-spacing:.14em;
    text-transform:uppercase; color:var(--ink-3); margin:0;
  }
  h1 {
    font-family:var(--display); font-weight:400; margin:0;
    font-size:clamp(1.6rem,3.4vw,2.3rem); line-height:1.1;
    letter-spacing:-.015em; text-wrap:balance;
  }
  h1 em { font-style:italic; color:var(--accent-ink); }
  .sub { margin:.15rem 0 0; color:var(--ink-2); font-size:.88rem; max-width:72ch; }

  /* ---- filter bar ---- */
  .filters {
    display:flex; flex-wrap:wrap; align-items:center; gap:.4rem .45rem;
    padding:.65rem .8rem; background:var(--surface); border-radius:8px;
    box-shadow:var(--shadow);
  }
  .flabel {
    font-family:var(--mono); font-size:.62rem; letter-spacing:.12em;
    text-transform:uppercase; color:var(--ink-3); margin-right:.1rem;
  }
  .fgroup { display:flex; flex-wrap:wrap; gap:.3rem; align-items:center; }
  .fsep { width:1px; align-self:stretch; background:var(--rule); margin:0 .45rem; }
  button.chip {
    font:inherit; font-size:.75rem; line-height:1.2;
    padding:.26rem .58rem; border-radius:999px; cursor:pointer;
    border:1px solid var(--rule); background:transparent; color:var(--ink-2);
  }
  button.chip:hover { border-color:var(--accent); color:var(--accent-ink); }
  button.chip:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  button.chip.on {
    background:var(--accent-soft); border-color:var(--accent);
    color:var(--accent-ink); font-weight:600;
  }
  .tally {
    margin-left:auto; font-family:var(--mono); font-size:.72rem;
    color:var(--ink-3); font-variant-numeric:tabular-nums;
  }
  .tally b { color:var(--ink); }

  /* ---- year + quarter grid ---- */
  .year { display:flex; flex-direction:column; gap:.55rem; }
  .yh { display:flex; align-items:baseline; gap:.7rem; }
  .yh h2 {
    font-family:var(--display); font-weight:400; margin:0;
    font-size:1.45rem; letter-spacing:-.01em;
  }
  .ym {
    font-family:var(--mono); font-size:.7rem; color:var(--ink-3);
    font-variant-numeric:tabular-nums;
  }
  .grid {
    display:grid; gap:.65rem;
    grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));
  }
  article.q {
    background:var(--surface); border-radius:8px; box-shadow:var(--shadow);
    padding:.65rem .75rem .75rem; display:flex; flex-direction:column; gap:.4rem;
    border-top:2px solid transparent;
  }
  /* The quarter we are actually in — the only reason the grid reads as a
     position in the year rather than a flat archive. */
  article.q.is-now { border-top-color:var(--accent); }
  article.q header {
    display:flex; align-items:baseline; gap:.45rem;
    padding-bottom:.3rem; border-bottom:1px solid var(--rule-2);
  }
  article.q h3 {
    margin:0; font-family:var(--mono); font-size:.78rem; font-weight:700;
    letter-spacing:.04em; color:var(--ink);
  }
  article.q.is-now h3 { color:var(--accent-ink); }
  .qm { font-family:var(--mono); font-size:.64rem; color:var(--ink-3); }
  .qn {
    margin-left:auto; font-family:var(--mono); font-size:.64rem;
    color:var(--ink-3); font-variant-numeric:tabular-nums;
  }

  ul.items { list-style:none; margin:0; padding:0;
             display:flex; flex-direction:column; gap:.26rem; }
  li.item {
    display:flex; align-items:baseline; gap:.3rem; flex-wrap:wrap;
    font-size:.81rem; line-height:1.3;
  }
  li.item a {
    color:var(--ink); text-decoration:none; font-weight:600;
    border-bottom:1px solid transparent;
  }
  li.item a:hover, li.item a:focus-visible {
    color:var(--accent-ink); border-bottom-color:var(--accent);
  }
  li.item a:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  li.item .d {
    font-family:var(--mono); font-size:.71rem; color:var(--ink-3);
    font-variant-numeric:tabular-nums; white-space:nowrap;
  }
  li.item .h {
    font-family:var(--mono); font-size:.6rem; letter-spacing:.06em;
    text-transform:uppercase; padding:.05rem .3rem; border-radius:3px;
    background:var(--amber-soft); color:var(--amber);
  }
  /* Held meetings recede but stay legible — the page is history plus outlook. */
  li.item.gone a { color:var(--ink-3); font-weight:500; }
  li.item.gone .d { opacity:.75; }
  li.item[hidden] { display:none; }
  p.empty {
    margin:.1rem 0 0; font-family:var(--mono); font-size:.68rem;
    color:var(--ink-3); opacity:.7;
  }
  p.empty[hidden] { display:none; }

  footer {
    border-top:1px solid var(--rule); padding-top:.85rem; margin-top:.4rem;
    font-size:.73rem; color:var(--ink-3);
    display:flex; flex-direction:column; gap:.25rem;
  }
  footer code { font-family:var(--mono); font-size:.71rem; color:var(--ink-2); }

  @media (max-width:34rem) {
    .tally { margin-left:0; width:100%; }
    .fsep { display:none; }
  }
</style>

<div class="wrap">
  <header class="top">
    <p class="eyebrow">Analyst Days &middot; conference calendar</p>
    <h1>The meetings that move <em>healthcare</em></h1>
    <p class="sub">Every conference on the tracked circuit for %%SPAN%%, by quarter.
      Dates are web-verified; hover a meeting for its full name, venue and why it
      matters. These are the meetings themselves, not our companies' appearances at
      them.</p>
  </header>

  <div class="filters">
    <span class="flabel">Sector</span>
    <span class="fgroup">%%SECTOR_CHIPS%%</span>
    <span class="fsep"></span>
    <span class="flabel">Type</span>
    <span class="fgroup">%%KIND_CHIPS%%</span>
    <span class="tally"><b id="shown">%%TOTAL%%</b> of %%TOTAL%% shown &middot;
      %%HELD%% held</span>
  </div>

  %%YEARS%%

  <footer>
    <span>Curated in <code>analyst-days/src/seed_conferences.py</code> &mdash; that
      constant is the source of truth; the database is a projection of it.</span>
    <span>Generated %%GENERATED%% from <code>data/events.db</code> via
      <code>scripts/build_conference_page.py</code>.</span>
  </footer>
</div>

<script>
(function () {
  var state = { sector: "all", kind: "all" };
  var items = Array.prototype.slice.call(document.querySelectorAll("li.item"));
  var cards = Array.prototype.slice.call(document.querySelectorAll("article.q"));
  var years = Array.prototype.slice.call(document.querySelectorAll("section.year"));
  var shown = document.getElementById("shown");

  function matches(el) {
    return (state.sector === "all" || el.dataset.sector === state.sector) &&
           (state.kind === "all" || el.dataset.kind === state.kind);
  }

  function apply() {
    var total = 0;
    items.forEach(function (el) {
      var ok = matches(el);
      el.hidden = !ok;
      if (ok) { total++; }
    });
    // Per-quarter counts, and an explicit "none" so a filtered-empty box reads as
    // empty rather than as an unexplained gap.
    cards.forEach(function (card) {
      var n = card.querySelectorAll("li.item:not([hidden])").length;
      card.querySelector("[data-count]").textContent = n;
      card.querySelector("p.empty").hidden = (n !== 0);
    });
    years.forEach(function (sec) {
      sec.querySelector("[data-year-count]").textContent =
        sec.querySelectorAll("li.item:not([hidden])").length;
    });
    shown.textContent = total;
  }

  Array.prototype.forEach.call(document.querySelectorAll("button.chip"),
    function (btn) {
      btn.addEventListener("click", function () {
        var g = btn.dataset.group;
        state[g] = btn.dataset.value;
        Array.prototype.forEach.call(
          document.querySelectorAll('button.chip[data-group="' + g + '"]'),
          function (b) {
            var on = (b === btn);
            b.classList.toggle("on", on);
            b.setAttribute("aria-pressed", on ? "true" : "false");
          });
        apply();
      });
    });

  apply();
})();
</script>
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
