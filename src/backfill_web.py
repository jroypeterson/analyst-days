"""Web-search pass of the historical backfill: the events EDGAR cannot see.

    python -m src.backfill_web --db data/backfill_2yr.db \
        --event-start 2024-11-14 --event-end 2026-11-14

`src/backfill.py` (2026-11-14) found 61 analyst / investor / R&D / capital markets days
on EDGAR and named its own blind spots: a day announced only on an investor-relations
calendar or in an unfiled press release (Humana's and Insulet's 2025 days), and the 58
scan-set names with no SEC registration at all (Roche, Lonza, CSL, the Japanese and
Nordic names). This pass asks Codex -- JP's ChatGPT subscription, the same search the
weekly run uses -- for every such day each company HELD in the window, then checks each
answer the way the weekly run does. JP 2026-11-14: "do all the things you recommend".

⛑ **The model finds; code decides.** An answer is a claim (type, date, URL). A row
becomes `historical` only when ALL of these hold, each checked by code on the fetched
page, never on the model's words:

  1. the cited page could be read, and the date is written on it (`date_grounding`,
     which reads non-English dates in their own language);
  2. the page names the event (the weekly trigger phrases) -- or, for a page with no
     English event name, it stays `discovered`;
  3. the page names the company;
  4. the source is the company's own site, an SEC filing or a press-release wire.
     A news article or a transcript site is a lead, never proof: `discovered`.

There is no classifier call and no model-assigned confidence here. The stored confidence
is a rule: 0.90 when checks 1-3 pass, 0.70 otherwise (under every marquee bar).

Like `src/backfill.py` it imports no output channel, refuses the production database
and GitHub Actions, and writes only `status='historical'` / `'discovered'` rows.
Resumable: every Codex reply is appended to the answers log and a re-run skips the
batches already answered. A subscription usage limit stops the pass at once.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from scripts import codex_web_search as codex
from src.backfill import BackfillRefused, assert_safe_db
from src.discovery import web_answers
from src.discovery.date_grounding import date_grounded_in_text
from src.discovery.scan_edgar import TRIGGER_RE
from src.state.events_repo import (
    PUSHABLE_EVENT_TYPES,
    CandidateEvent,
    CandidateSource,
    upsert_event,
)
from src.state.schema import init_db
from src.universe import load_scan_universe

DEFAULT_DB = "data/backfill_2yr.db"
DEFAULT_ANSWERS_LOG = "data/backfill_web_answers.jsonl"
DEFAULT_STATS = "data/backfill_web_stats.json"
BATCH_SIZE = 8
CONF_CHECKED, CONF_LEAD = 0.90, 0.70

WIRE_HOSTS = ("businesswire.com", "prnewswire.com", "globenewswire.com", "newsfilecorp.com",
              "accesswire.com", "newswire.ca")
# Hosting used by many companies' OWN investor sites. The address proves nothing about
# whose page it is, so these count as the company's only with the company named on the page.
IR_HOSTING = ("q4cdn.com", "q4inc.com", "q4web.com", "gcs-web.com", "investorroom.com",
              "mediaroom.com", "cision.com")
_LEGAL = {"inc", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "ag", "sa", "se",
          "nv", "holdings", "holding", "group", "the", "and", "adr", "class", "as", "ab", "oyj",
          "spa", "kk", "llc", "lp", "trust"}


def history_prompt(companies: list[tuple[str, str]], start: date, end: date) -> str:
    listing = "\n".join(f"- {t}: {name}" for t, name in companies)
    return (
        f"Use web search. For EACH company below, list EVERY Investor Day, Analyst Day, R&D Day or "
        f"Capital Markets Day that the company ITSELF hosted between {start.isoformat()} and "
        f"{end.isoformat()} inclusive. Do not count earnings calls, annual shareholder meetings, "
        "roadshows, product launches, or appearances at third-party / bank conferences. For each "
        "event give the address of a page that itself states the date - the company's own "
        "investor-relations page, its press release, or its SEC filing is best; the company's own "
        "language is fine. Do not run shell commands or read files; only search the web.\n\n"
        f"{listing}\n\n"
        "Reply with ONLY a JSON object keyed by the ticker exactly as written above. Each value is "
        'a list of {"event_type": "investor_day" | "analyst_day" | "rd_day" | '
        '"capital_markets_day", "date": "YYYY-MM-DD", "source_url": "..."} - an empty list when '
        "you find none. Give only events whose exact date you found. Include every ticker."
    )


def parse_history(text: str, tickers: list[str]) -> dict[str, list[web_answers.Answer]]:
    """{ticker: [Answer, ...]} for every asked ticker the reply covers; a ticker the reply
    leaves out is ABSENT (not searched), an empty list is 'searched, none found'."""
    m = re.search(r"\{.*\}", re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip()), re.S)
    if not m:
        raise web_answers.AnswerParseError("no JSON object in the reply: %r" % (text or "")[:200])
    try:
        doc = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise web_answers.AnswerParseError(f"reply is not valid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise web_answers.AnswerParseError("reply is not a JSON object")
    out: dict[str, list[web_answers.Answer]] = {}
    for ticker in tickers:
        v = doc.get(ticker, "absent")
        if v is None:
            v = []
        if not isinstance(v, list):
            continue
        events = []
        for e in v:
            if not isinstance(e, dict):
                continue
            one = web_answers.parse_answers(json.dumps({ticker: e}), [ticker]).get(ticker)
            if one is not None and one.date and one.event_type in PUSHABLE_EVENT_TYPES:
                events.append(one)
        out[ticker] = events
    return out


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _registrable(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "ne", "or", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def name_tokens(company_name: str) -> list[str]:
    """The distinctive words of a company name, for 'does this page name the company'."""
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ0-9]+", company_name or "") if len(w) >= 3]
    return [w.lower() for w in words if w.lower() not in _LEGAL][:2]


def names_company(text: str, company_name: str) -> bool:
    tokens = name_tokens(company_name)
    low = text.lower()
    return bool(tokens) and re.search(r"\b" + re.escape(tokens[0]) + r"\b", low) is not None


def source_type(url: str, website: Optional[str], company_named: bool) -> str:
    """`8K` / `IR_PAGE` / `PRESS_RELEASE` (authoritative) or `TAVILY_HIT` (a lead only)."""
    host = _host(url)
    if host.endswith("sec.gov"):
        return "8K"
    if any(host == w or host.endswith("." + w) for w in WIRE_HOSTS):
        return "PRESS_RELEASE" if company_named else "TAVILY_HIT"
    site = _host(website or "")
    if site and _registrable(host) == _registrable(site):
        return "IR_PAGE"
    if company_named and any(host == h or host.endswith("." + h) for h in IR_HOSTING):
        return "IR_PAGE"
    return "TAVILY_HIT"


def check_answer(answer: web_answers.Answer, company_name: str, website: Optional[str], *,
                 fetch: Optional[Callable] = None) -> dict:
    """Everything code can establish about one claimed event, from the cited page."""
    fetch = fetch or web_answers.fetch_text
    out = {"url": answer.source_url, "read": False, "date_on_page": False, "event_named": False,
           "company_named": False, "source_type": "TAVILY_HIT", "snippet": "", "error": ""}
    try:
        title, text = fetch(answer.source_url)
    except web_answers.FetchError as e:
        out["error"] = str(e)[:200]
        return out
    snippet = web_answers.cut_snippet(text, answer.date)
    out.update(read=True, snippet=snippet,
               date_on_page=date_grounded_in_text(answer.date, f"{title} {snippet}"),
               event_named=bool(TRIGGER_RE.search(f"{title} {snippet}")),
               company_named=names_company(f"{title} {text}", company_name))
    out["source_type"] = source_type(answer.source_url, website, out["company_named"])
    return out


def _existing_type(conn, ticker: str, start_date: str) -> Optional[str]:
    """One marquee day per company per date: if the EDGAR pass (or an earlier answer) already
    holds this day under another type, keep that type instead of adding a second row."""
    row = conn.execute(
        "SELECT event_type FROM events WHERE ticker = ? AND start_date = ? AND status NOT IN "
        "('cancelled', 'superseded') ORDER BY (status = 'historical') DESC, id LIMIT 1",
        (ticker, start_date)).fetchone()
    return row[0] if row else None


def store(conn, ticker: str, company_name: str, website: Optional[str],
          answers: list[web_answers.Answer], start: date, end: date, *, today: date,
          fetch: Optional[Callable] = None, log: Callable[[str], None] = print) -> dict:
    c = {"claimed": 0, "out_of_window": 0, "historical": 0, "discovered": 0, "merged": 0}
    past_ref = (min(end, today) + timedelta(days=1)).isoformat()
    lo, hi = start.isoformat(), min(end, today).isoformat()
    for a in answers:
        c["claimed"] += 1
        if not (lo <= a.date <= hi):
            c["out_of_window"] += 1
            continue
        chk = check_answer(a, company_name, website, fetch=fetch)
        checked = chk["read"] and chk["date_on_page"] and chk["event_named"] and chk["company_named"]
        etype = _existing_type(conn, ticker, a.date) or a.event_type
        cand = CandidateEvent(
            ticker=ticker, company_name=company_name, event_type=etype, start_date=a.date,
            end_date=None, multi_day=False, date_imprecise=False, imprecise_hint=None,
            confidence=CONF_CHECKED if checked else CONF_LEAD,
            date_grounded=bool(chk["date_on_page"]),
            sources=[CandidateSource(
                source_type=chk["source_type"], source_url=a.source_url,
                source_excerpt=(chk["snippet"] or chk["error"] or "")[:500])])
        _, status, is_new = upsert_event(conn, cand, today_iso=past_ref, historical=True)
        c["historical" if status == "historical" else "discovered"] += 1
        c["merged"] += 0 if is_new else 1
        why = "" if status == "historical" else " [" + ", ".join(
            n for n, ok in (("page unreadable", chk["read"]), ("date not on page", chk["date_on_page"]),
                            ("event not named", chk["event_named"]),
                            ("company not named", chk["company_named"]),
                            ("not an authoritative source", chk["source_type"] != "TAVILY_HIT"))
            if not ok) + "]"
        log(f"  {ticker:8} {etype:20} {a.date} {chk['source_type']:13} -> {status}"
            f"{'' if is_new else ' (merged)'}{why}  {a.source_url[:80]}")
    return c


def _load_log(path: Path) -> dict[str, dict]:
    done: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                done[rec["key"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def run(args: argparse.Namespace, *, ask: Optional[Callable] = None,
        fetch: Optional[Callable] = None, log: Callable[[str], None] = print) -> int:
    db_path = assert_safe_db(args.db)
    start, end = date.fromisoformat(args.event_start), date.fromisoformat(args.event_end)
    today = date.today()
    universe = load_scan_universe()
    if args.tickers:
        wanted = {t.upper() for t in args.tickers}
        universe = [t for t in universe if t.ticker.upper() in wanted]
    if args.limit:
        universe = universe[:args.limit]
    meta = {t.ticker: t for t in universe}
    batches = [universe[i:i + BATCH_SIZE] for i in range(0, len(universe), BATCH_SIZE)]
    log_path = Path(args.answers_log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    done = _load_log(log_path)
    ask = ask or codex.ask_codex
    conn = None if args.dry_run else init_db(str(db_path))

    totals = {"names": len(universe), "names_answered": 0, "names_not_searched": 0, "calls": 0,
              "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "claimed": 0,
              "out_of_window": 0, "historical": 0, "discovered": 0, "merged": 0, "stopped": ""}
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="codex-history-") as workdir:
        for n, batch in enumerate(batches, 1):
            tickers = [t.ticker for t in batch]
            key = f"{start}|{end}|" + ",".join(tickers)
            rec = done.get(key)
            if rec is None and not totals["stopped"]:
                prompt = history_prompt([(t.ticker, t.company_name) for t in batch], start, end)
                reply = None
                for attempt in (1, 2):
                    try:
                        reply, usage = ask(prompt, workdir=workdir)
                        totals["calls"] += 1
                        for k in ("input_tokens", "cached_input_tokens", "output_tokens"):
                            totals[k] += int(usage.get(k) or 0)
                        parse_history(reply, tickers)      # must parse to be logged
                        break
                    except (codex.QuotaExhausted, codex.SearchUnavailable) as e:
                        totals["stopped"] = str(e)[:300]
                        reply = None
                        break
                    except (codex.BatchFailed, web_answers.AnswerParseError) as e:
                        reply = None
                        log(f"[history] batch {n}/{len(batches)} attempt {attempt} failed: {str(e)[:160]}")
                        if attempt == 1:
                            time.sleep(5)
                if reply is not None:
                    rec = {"key": key, "tickers": tickers, "reply": reply,
                           "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec) + "\n")
            if rec is None:
                totals["names_not_searched"] += len(tickers)
                continue
            parsed = parse_history(rec["reply"], tickers)
            totals["names_answered"] += len(parsed)
            totals["names_not_searched"] += len(tickers) - len(parsed)
            log(f"[history] batch {n}/{len(batches)}: {len(parsed)}/{len(tickers)} answered, "
                f"{sum(len(v) for v in parsed.values())} event(s) claimed")
            if conn is None:
                continue
            for ticker, answers in parsed.items():
                t = meta[ticker]
                c = store(conn, ticker, t.company_name, t.website, answers, start, end,
                          today=today, fetch=fetch, log=log)
                for k, v in c.items():
                    totals[k] += v
            conn.commit()
    if conn is not None:
        conn.close()
    totals["seconds"] = round(time.time() - t0)
    Path(args.stats).write_text(json.dumps(totals, indent=1) + "\n", encoding="utf-8")
    log("[history] " + json.dumps(totals))
    if totals["stopped"]:
        log("[history] STOPPED: " + totals["stopped"])
        return 1
    return 1 if totals["names_not_searched"] else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--event-start", required=True)
    p.add_argument("--event-end", required=True)
    p.add_argument("--tickers", nargs="*")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true", help="ask and log, write no database row")
    p.add_argument("--answers-log", default=DEFAULT_ANSWERS_LOG)
    p.add_argument("--stats", default=DEFAULT_STATS)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except BackfillRefused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
