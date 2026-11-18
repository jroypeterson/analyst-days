"""Render the comprehensive conference inventory to a self-contained page.

    python scripts/build_conference_inventory.py [--out PATH] [--today YYYY-MM-DD]

One row per conference SERIES (not per year): what the name stands for, who attends,
whether it is the main meeting for that group or one of several, attendance, and the
next dated instance. This is the page both conference channels bookmark
(`conference_calendar.INVENTORY_URL`). Published as a Claude Artifact; re-run and
re-publish to the SAME artifact URL so the bookmark keeps working.

It reads `src/seed_conferences.py` directly, never `data/events.db`: the list is the
source of truth and the local DB is a stale sandbox.

Self-contained by requirement: the artifact CSP blocks external hosts, so all CSS and
JS are inline. The page is written without <html>/<head>/<body>; the artifact service
wraps it.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from src import seed_conferences as sc  # noqa: E402
from src.conference_briefs import fmt_dates  # noqa: E402

DEFAULT_OUT = os.path.join(REPO, "exports", "conference_inventory.html")

CATEGORY_ORDER = (
    "Medical specialty meetings", "Nursing facilities & senior living",
    "Home health & hospice", "Labs & diagnostics", "Pharmacy & distribution",
    "Hospitals & physician practices", "Payers", "Health tech", "Medtech industry",
    "Dental", "Veterinary", "Bank conferences",
)


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _slug(s: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in s)


def series_rows(today: date) -> list[dict]:
    """One dict per series: its facts plus the next and the last dated instance."""
    by_series: dict[str, list] = {}
    for c in sc.CONFERENCES:
        if c.start and c.end:
            by_series.setdefault(c.series, []).append(c)
    rows = []
    for slug_, m in sc.SERIES_META.items():
        inst = sorted(by_series.get(slug_, []), key=lambda c: c.start)
        ahead = [c for c in inst if date.fromisoformat(c.end) >= today]
        held = [c for c in inst if date.fromisoformat(c.end) < today]
        nxt = ahead[0] if ahead else None
        last = held[-1] if held else None
        any_inst = nxt or last
        label = (any_inst.short_name.rsplit(" ", 1)[0] if any_inst else m.full_name)
        rows.append({
            "slug": slug_, "label": label, "meta": m, "next": nxt, "last": last,
            "url": (nxt or last).url if any_inst else "",
            "channel": ("wallst" if m.category == "Bank conferences" else "industry"),
            "sort": nxt.start if nxt else "9999",
        })
    return rows


def build(today: date) -> str:
    rows = series_rows(today)
    cats = [c for c in CATEGORY_ORDER if any(r["meta"].category == c for r in rows)]
    cats += sorted({r["meta"].category for r in rows} - set(cats))
    n_dated = sum(1 for r in rows if r["next"])

    chips = ['<button class="chip on" data-value="all" aria-pressed="true">All</button>']
    for c in cats:
        n = sum(1 for r in rows if r["meta"].category == c)
        chips.append(f'<button class="chip" data-value="{_slug(c)}" aria-pressed="false">'
                     f'{_esc(c)} <span class="n">{n}</span></button>')

    sections = []
    for c in cats:
        items = sorted((r for r in rows if r["meta"].category == c),
                       key=lambda r: (r["sort"], r["label"]))
        cards = []
        for r in items:
            m, nxt, last = r["meta"], r["next"], r["last"]
            if nxt:
                running = date.fromisoformat(nxt.start) <= today
                when = (f'<span class="when{" now" if running else ""}">'
                        f'{_esc(fmt_dates(nxt.start, nxt.end))}</span>'
                        f'<span class="loc">{_esc(nxt.location or "location not confirmed")}'
                        f'{" · under way" if running else ""}</span>')
            else:
                when = '<span class="when tbd">Next date not published</span>'
                if last:
                    when += (f'<span class="loc">last held {_esc(fmt_dates(last.start, last.end))}'
                             f'{", " + _esc(last.location) if last.location else ""}</span>')
            name = (f'<a href="{_esc(r["url"])}" target="_blank" rel="noopener">{_esc(r["label"])}</a>'
                    if r["url"].startswith("http") else _esc(r["label"]))
            cards.append(
                f'<article class="row" data-cat="{_slug(m.category)}">'
                f'<div class="c-name"><h3>{name}</h3><p class="full">{_esc(m.full_name)}</p>'
                f'<p class="org">{_esc(m.organizer)}</p></div>'
                f'<div class="c-group"><span class="k">Group</span>{_esc(m.category)}</div>'
                f'<div class="c-who"><p><span class="k">For</span>{_esc(m.audience)}</p>'
                f'<p><span class="k">Standing</span>{_esc(m.standing)}</p></div>'
                f'<div class="c-size"><span class="k">Size</span>'
                f'{_esc(m.attendance) if m.attendance else "<span class=\'tbd\'>not published</span>"}</div>'
                f'<div class="c-when"><span class="k">Next</span>{when}</div>'
                f'</article>')
        sections.append(
            f'<section class="cat" data-cat="{_slug(c)}"><div class="ch"><h2>{_esc(c)}</h2>'
            f'<span class="cm">{len(items)} series · posts to '
            f'{"#conferences-wall-st" if c == "Bank conferences" else "#conferences-industry"}'
            f'</span></div><div class="rows">{"".join(cards)}</div></section>')

    return _PAGE.format(
        n=len(rows), n_dated=n_dated, n_undated=len(rows) - n_dated,
        today=today.isoformat(), chips="".join(chips), sections="".join(sections))


_PAGE = """<title>Healthcare Conference Circuit</title>
<meta name="claude:viewport-fit-sides" content="cover">
<style>
  /* Layout: a filter bar, then one band per group; each series is a five-column row
     (name / group / who and standing / size / next date) that stacks on a phone. */
  :root {{
    --ground:#F4F7F5; --surface:#FFFFFF; --surface-2:#EBF1EE;
    --ink:#0F1618; --ink-2:#3D4B4C; --ink-3:#6B7B79;
    --rule:#D7E1DD; --rule-2:#E8EEEB;
    --accent:#0B6A56; --accent-ink:#0B6A56; --accent-soft:#DCEBE5;
    --amber:#855312; --amber-soft:#F3E8D6;
    --shadow:0 1px 2px rgba(15,22,24,.05), 0 6px 18px -12px rgba(15,22,24,.22);
    --display:Georgia,"Iowan Old Style","Palatino Linotype",Palatino,serif;
    --body:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --ground:#0B1113; --surface:#121A1C; --surface-2:#0F1719;
      --ink:#E4ECE9; --ink-2:#B3C3BF; --ink-3:#849692;
      --rule:#223032; --rule-2:#1A2527;
      --accent:#52C0A0; --accent-ink:#7FD3B9; --accent-soft:#12332C;
      --amber:#D9A860; --amber-soft:#2E2413;
      --shadow:0 1px 2px rgba(0,0,0,.4), 0 6px 18px -12px rgba(0,0,0,.8);
      color-scheme:dark;
    }}
  }}
  :root[data-theme="dark"] {{
    --ground:#0B1113; --surface:#121A1C; --surface-2:#0F1719;
    --ink:#E4ECE9; --ink-2:#B3C3BF; --ink-3:#849692;
    --rule:#223032; --rule-2:#1A2527;
    --accent:#52C0A0; --accent-ink:#7FD3B9; --accent-soft:#12332C;
    --amber:#D9A860; --amber-soft:#2E2413;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 6px 18px -12px rgba(0,0,0,.8);
    color-scheme:dark;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--ground); color:var(--ink); font-family:var(--body);
         font-size:15px; line-height:1.45; -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:76rem; margin:0 auto; padding-block:clamp(1.5rem,3vw,2.5rem) 3rem;
          padding-left:max(16px, env(safe-area-inset-left, 0px));
          padding-right:max(16px, env(safe-area-inset-right, 0px));
          display:flex; flex-direction:column; gap:1.4rem; }}
  header.top {{ display:flex; flex-direction:column; gap:.35rem; }}
  .eyebrow, .k, .cm, .flabel {{ font-family:var(--mono); font-size:.64rem; letter-spacing:.12em;
          text-transform:uppercase; color:var(--ink-3); }}
  .eyebrow {{ margin:0; }}
  h1 {{ font-family:var(--display); font-weight:400; margin:0; text-wrap:balance;
       font-size:clamp(1.6rem,3.4vw,2.3rem); line-height:1.1; letter-spacing:-.015em; }}
  h1 em {{ font-style:italic; color:var(--accent-ink); }}
  .sub {{ margin:.15rem 0 0; color:var(--ink-2); font-size:.9rem; max-width:72ch; }}
  .filters {{ display:flex; flex-wrap:wrap; align-items:center; gap:.35rem .4rem;
             padding:.65rem .8rem; background:var(--surface); border-radius:8px;
             box-shadow:var(--shadow); }}
  .flabel {{ margin-right:.2rem; }}
  button.chip {{ font:inherit; font-size:.75rem; line-height:1.2; padding:.26rem .58rem;
                border-radius:999px; cursor:pointer; border:1px solid var(--rule);
                background:transparent; color:var(--ink-2); }}
  button.chip .n {{ font-family:var(--mono); font-size:.66rem; color:var(--ink-3); }}
  button.chip:hover {{ border-color:var(--accent); color:var(--accent-ink); }}
  button.chip:focus-visible, a:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
  button.chip.on {{ background:var(--accent-soft); border-color:var(--accent);
                   color:var(--accent-ink); font-weight:600; }}
  .tally {{ margin-left:auto; font-family:var(--mono); font-size:.72rem; color:var(--ink-3);
           font-variant-numeric:tabular-nums; }}
  .tally b {{ color:var(--ink); }}
  .cat {{ display:flex; flex-direction:column; gap:.5rem; }}
  .ch {{ display:flex; align-items:baseline; gap:.7rem; flex-wrap:wrap; }}
  .ch h2 {{ font-family:var(--display); font-weight:400; margin:0; font-size:1.3rem; }}
  .rows {{ background:var(--surface); border-radius:8px; box-shadow:var(--shadow); }}
  article.row {{ display:grid; gap:.3rem 1.2rem; padding:.7rem .85rem;
                grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr) minmax(0,2fr) minmax(0,.9fr) minmax(0,1.05fr);
                border-top:1px solid var(--rule-2); }}
  article.row:first-child {{ border-top:0; }}
  article.row > div {{ min-width:0; }}
  article.row h3 {{ margin:0; font-size:.95rem; font-weight:650; }}
  article.row h3 a {{ color:var(--ink); text-decoration:none; border-bottom:1px solid var(--rule); }}
  article.row h3 a:hover {{ color:var(--accent-ink); border-bottom-color:var(--accent); }}
  article.row p {{ margin:0; font-size:.82rem; color:var(--ink-2); }}
  .full {{ color:var(--ink) !important; }}
  .org {{ color:var(--ink-3) !important; font-size:.76rem !important; }}
  .c-who {{ display:flex; flex-direction:column; gap:.25rem; }}
  .k {{ display:inline-block; min-width:4.6rem; margin-right:.3rem; }}
  .c-group, .c-size, .c-when {{ font-size:.82rem; color:var(--ink-2); }}
  .c-group .k, .c-size .k, .c-when .k {{ display:block; margin-bottom:.1rem; }}
  .c-group {{ color:var(--accent-ink); font-weight:600; }}
  .when {{ display:block; font-weight:600; color:var(--ink); font-variant-numeric:tabular-nums; }}
  .when.now {{ color:var(--accent-ink); }}
  .loc {{ display:block; font-size:.76rem; color:var(--ink-3); }}
  .tbd {{ color:var(--amber); font-weight:500; }}
  footer {{ border-top:1px solid var(--rule); padding-top:.85rem; font-size:.74rem;
           color:var(--ink-3); display:flex; flex-direction:column; gap:.25rem; max-width:80ch; }}
  footer code {{ font-family:var(--mono); font-size:.71rem; color:var(--ink-2); }}
  @media (max-width:52rem) {{
    article.row {{ grid-template-columns:minmax(0,1fr) minmax(0,1fr); }}
    .c-name, .c-group, .c-who {{ grid-column:1 / -1; }}
    .tally {{ margin-left:0; width:100%; }}
  }}
</style>

<div class="wrap">
  <header class="top">
    <p class="eyebrow">Analyst Days &middot; conference inventory</p>
    <h1>The meetings that move <em>healthcare</em></h1>
    <p class="sub">Every conference series tracked for the two Slack channels: who attends,
      whether it is the main meeting for that group or one of several, how big it is, and
      when it next meets. {n} series; {n_dated} have a confirmed next date and
      {n_undated} have not published one. As of {today}.</p>
  </header>

  <div class="filters">
    <span class="flabel">Group</span>
    {chips}
    <span class="tally"><b id="shown">{n}</b> of {n} shown</span>
  </div>

  {sections}

  <footer>
    <span>Dates were confirmed by web search, most from the organizer's own page. A date
      that could not be confirmed is shown as not published; none is estimated.</span>
    <span>Attendance is the organizer's or trade press's figure where one exists, with its
      year. Bank conferences rarely publish one.</span>
    <span>Source of truth: <code>analyst-days/src/seed_conferences.py</code> and
      <code>src/conference_circuit.json</code>. Generated {today} by
      <code>scripts/build_conference_inventory.py</code>.</span>
  </footer>
</div>

<script>
(function () {{
  var chips = Array.prototype.slice.call(document.querySelectorAll("button.chip"));
  var rows = Array.prototype.slice.call(document.querySelectorAll("article.row"));
  var cats = Array.prototype.slice.call(document.querySelectorAll("section.cat"));
  var shown = document.getElementById("shown");
  function apply(value) {{
    var total = 0;
    cats.forEach(function (sec) {{
      var on = (value === "all" || sec.dataset.cat === value);
      sec.hidden = !on;
      if (on) {{ total += sec.querySelectorAll("article.row").length; }}
    }});
    shown.textContent = total;
    chips.forEach(function (b) {{
      var on = (b.dataset.value === value);
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }});
  }}
  chips.forEach(function (b) {{
    b.addEventListener("click", function () {{ apply(b.dataset.value); }});
  }});
  apply("all");
}})();
</script>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--today", default=date.today().isoformat())
    args = ap.parse_args(argv)
    html = build(date.fromisoformat(args.today))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {args.out} ({len(html)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
