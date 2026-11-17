"""Pre-conference alerts and post-conference summaries.

    python -m src.cli --conference-briefs [--dry-run] [--only "AHA 2026"] [--phase post]

JP 2026-11-17: *"I want to know what topics are on the expert minds and if there are
any new topics or topics that are no longer coming up. ideally you'd let me know
before hand for key medical conferences where something significant is happening ...
and then afterwards for most conferences provide a 4 line at most summary"*.

Two phases per conference instance:
  pre   scientific meetings only, in the 7 days before the start. Posted ONLY when
        the research finds something significant; a quiet meeting is recorded and
        not posted.
  post  2 to 21 days after the end, for every conference whose series has
        post_summary on (all industry meetings; of the bank conferences, JPM).

THE MODEL RESEARCHES, CODE RENDERS. The model (web search) returns a JSON brief; the
Slack text is built here, so the layout JP approved on 2026-11-17 cannot drift with
the model's mood: a summary of at most 4 bullets, one entry per finding with its
details behind the quote bar, each detail carrying the number a clinician or investor
would judge it by, then the links.

STATE IS A COMMITTED FILE, not the database: `exports/conference_briefs/<slug>.<phase>.json`.
events.db is an expiring CI artifact; a lost artifact must not re-post (or re-buy) a
year of summaries. The file is written BEFORE the Slack post and stamped `posted_ts`
after, so a failed post retries the post without paying for the research twice. The
stored `topics` list is next year's baseline for "new / no longer coming up".
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from src import seed_conferences as sc
from src.outputs import slack_bot

REPO = Path(__file__).resolve().parents[1]
BRIEF_DIR = REPO / "exports" / "conference_briefs"

MODEL = "claude-opus-5-5"
REFUSAL_FALLBACK_MODEL = "claude-opus-5"
PRE_WINDOW_DAYS = 7          # alert when the meeting starts within this many days
POST_MIN_DAYS = 2            # coverage needs a couple of days to accumulate
POST_MAX_DAYS = 21           # older than this is no longer news; skip, do not backfill
MAX_BRIEFS_PER_RUN = 5       # spend guard; the rest are picked up by the next run
MAX_SUMMARY_BULLETS = 4      # JP: "a 4 line at most summary"
MAX_ITEMS = 7
MAX_LINES_PER_ITEM = 4


class BriefError(RuntimeError):
    pass


# --------------------------------------------------------------------------- due


@dataclass(frozen=True)
class Due:
    conf: sc.Conference
    phase: str  # "pre" | "post"


def slug(conf: sc.Conference) -> str:
    return re.sub(r"[^a-z0-9]+", "-", conf.short_name.lower()).strip("-")


def state_path(conf: sc.Conference, phase: str) -> Path:
    return BRIEF_DIR / f"{slug(conf)}.{phase}.json"


def load_state(conf: sc.Conference, phase: str) -> Optional[dict]:
    p = state_path(conf, phase)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _is_done(state: Optional[dict]) -> bool:
    """Done = posted, or deliberately not posted (a quiet pre-conference check)."""
    return bool(state and (state.get("posted_ts") or state.get("skipped_reason")))


def due_briefs(today: date, conferences=None) -> list[Due]:
    """Everything owed today, soonest-stale first (post before pre, oldest end first)."""
    conferences = sc.CONFERENCES if conferences is None else conferences
    post: list[Due] = []
    pre: list[Due] = []
    for c in conferences:
        if not (c.start and c.end):
            continue
        start, end = date.fromisoformat(c.start), date.fromisoformat(c.end)
        if sc.wants_post_summary(c):
            age = (today - end).days
            if POST_MIN_DAYS <= age <= POST_MAX_DAYS and not _is_done(load_state(c, "post")):
                post.append(Due(c, "post"))
        if sc.wants_pre_alert(c):
            lead = (start - today).days
            if 0 <= lead <= PRE_WINDOW_DAYS and not _is_done(load_state(c, "pre")):
                pre.append(Due(c, "pre"))
    post.sort(key=lambda d: d.conf.end)
    pre.sort(key=lambda d: d.conf.start)
    # Pre-alerts first: they expire when the meeting starts; a summary can wait a run.
    return pre + post


def prior_topics(conf: sc.Conference) -> tuple[Optional[str], list[str]]:
    """(prior instance short name, its stored topics) from the latest earlier brief."""
    earlier = sorted(
        (c for c in sc.CONFERENCES
         if c.series == conf.series and c.end and conf.end and c.end < conf.end),
        key=lambda c: c.end, reverse=True)
    for c in earlier:
        st = load_state(c, "post")
        if st and st.get("brief", {}).get("topics"):
            return c.short_name, list(st["brief"]["topics"])
    return (earlier[0].short_name if earlier else None), []


# ------------------------------------------------------------------------ prompt

_JSON_RULES = """Return ONE JSON object in a ```json fence as the last thing in your reply, and
nothing after it. Plain strings only inside it: no markdown, no bullets, no asterisks.
Every URL must be one you actually opened or saw in a search result."""

_EVIDENCE_RULE = """EVIDENCE: every detail line carries the number a clinician or an investor would
judge it by. For trial data: the endpoint, the effect size with its confidence interval or
p-value, and the number of patients (for example "MACE hazard ratio 0.99, 95% CI 0.88-1.11,
p=0.84, n=6,376"). For an operator or policy topic: the rate, dollar amount, percentage
or effective date. Use only figures you found in a source. If a source gave no figure,
write "figures not reported" rather than a number from memory. Give each number its unit."""


def _conference_facts(conf: sc.Conference) -> str:
    m = sc.meta_for(conf)
    return "\n".join([
        f"Conference: {conf.name} ({conf.short_name})",
        f"Organizer: {m.organizer}",
        f"Who attends: {m.audience}",
        f"Kind: {conf.kind}",
        f"Dates: {conf.start} to {conf.end}",
        f"Location: {conf.location or 'not recorded'}",
        f"Official site: {conf.url}",
    ])


def build_prompt(conf: sc.Conference, phase: str, today: date) -> str:
    facts = _conference_facts(conf)
    scientific = conf.kind == sc.SCIENTIFIC
    reader = ("The reader is a healthcare public-equity investor, not a clinician. He wants "
              "to know what moved, for which companies, and what the specialists who attend "
              "this meeting are focused on. Name the company and its ticker for every "
              "product. Expand any abbreviation the first time it appears.")
    if phase == "pre":
        return f"""Today is {today.isoformat()}. Research what is scheduled at this upcoming meeting.

{facts}

{reader}

Use web search to find the late-breaking sessions, embargoed or already-released
abstracts, company press releases announcing presentations, and preview coverage.

Decide whether anything SIGNIFICANT is expected: a registrational or pivotal readout,
first full data for a result already announced as a headline, a head-to-head or
practice-changing trial, or a first-in-human result in a crowded investor debate.
Routine posters and re-analyses are not significant. If nothing clears that bar, say so.

{_EVIDENCE_RULE} For a readout that has not happened, the "number" is what is already
known: the pre-announced headline result, the trial size, the primary endpoint, and the
session date and time.

{_JSON_RULES}
{{
  "significant": true or false,
  "why_now": ["at most 3 short bullets: why this meeting matters this year"],
  "watch": [
    {{"title": "Trial or topic, product (Company)", "tickers": ["NVO"],
      "when": "session day and time if known, else empty string",
      "lines": ["at most 3 detail lines of at most 30 words each, each with its number"],
      "url": "best single source"}}
  ],
  "sources": [{{"label": "what it is", "url": "https://..."}}],
  "caveat": "one sentence on what you could not verify, or empty string"
}}
At most {MAX_ITEMS} watch items, most important first."""

    prior_name, topics = prior_topics(conf)
    if topics:
        baseline = (f"Last year's brief for {prior_name} recorded these topics:\n- "
                    + "\n- ".join(topics)
                    + "\nCompare this year against that list.")
    elif prior_name:
        baseline = (f"There is no stored brief for {prior_name}. Search for coverage of that "
                    "earlier meeting and compare against it. If you cannot ground a "
                    "comparison in sources, return empty lists rather than guessing.")
    else:
        baseline = "There is no earlier instance on file. Return empty new/fading lists."
    what = ("the late-breaking and most-discussed data" if scientific else
            "what operators, executives and regulators said: reimbursement and rate "
            "changes, labor and staffing, regulation and enforcement, volumes and demand, "
            "technology adoption, deals. This is an industry meeting, not a data meeting")
    return f"""Today is {today.isoformat()}. This meeting has ended. Research what happened there.

{facts}

{reader}

Use web search to find {what}. Prefer the organizer's own newsroom, company press
releases, journal publications and specialist trade press over aggregators.

Then answer three things:
1. What happened that an investor must know (the summary and the items).
2. What was on the attendees' minds: the themes that dominated sessions and hallway
   conversation, per the meeting's leaders and trade-press wrap-ups.
3. What is NEW this year and what is NO LONGER coming up. {baseline}

{_EVIDENCE_RULE}

{_JSON_RULES}
{{
  "summary": ["at most {MAX_SUMMARY_BULLETS} bullets, most critical first. Each is ONE short sentence of at most 25 words: the takeaway and at most one number. Tickers, trial names and the full figures belong in the items, not here"],
  "items": [
    {{"title": "Trial or topic, product (Company)", "tickers": ["NVS"],
      "lines": ["at most {MAX_LINES_PER_ITEM} detail lines of at most 30 words each, each with its number"],
      "url": "best single source"}}
  ],
  "expert_minds": ["at most 4 short lines: the themes, and who said so"],
  "new_topics": ["topics prominent this year that were absent or minor last year"],
  "fading_topics": ["topics prominent last year that were absent or minor this year"],
  "topics": ["8 to 15 short topic tags for this year's meeting, for next year's comparison"],
  "sources": [{{"label": "what it is", "url": "https://..."}}],
  "caveat": "one sentence on what you could not verify, or empty string"
}}
At most {MAX_ITEMS} items, most important first."""


# -------------------------------------------------------------------- generation


def _extract_json(text: str) -> dict:
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = list(reversed(fences))
    if not candidates:
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j > i:
            candidates = [text[i:j + 1]]
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise BriefError("model reply held no parseable JSON object")


def _ask(client, model: str, prompt: str) -> tuple[str, int, dict]:
    """One researched answer. Returns (final text, web searches run, usage totals)."""
    tools = [
        # Budgets set from the first live run (AASLD 2026, 2026-11-17): 14 searches and
        # 8 uncapped page fetches cost 770,918 input tokens, about USD 3.30 at
        # USD 4 / USD 20 per million. Every fetched page is re-read on each later step
        # of the server-side loop, so the page cap matters more than the search count.
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 10},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4,
         "max_content_tokens": 5000},
    ]
    messages = [{"role": "user", "content": prompt}]
    searches = 0
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    for _ in range(6):  # initial request + at most 5 pause_turn resumes
        with client.messages.stream(
            model=model,
            max_tokens=32000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            tools=tools,
            messages=messages,
            cache_control={"type": "ephemeral"},   # a pause_turn resume re-reads the prefix
        ) as stream:
            resp = stream.get_final_message()
        for k in usage:
            usage[k] += getattr(resp.usage, k, 0) or 0
        searches += sum(1 for b in resp.content
                        if b.type == "server_tool_use" and b.name == "web_search")
        if resp.stop_reason == "pause_turn":
            messages = [{"role": "user", "content": prompt},
                        {"role": "assistant", "content": resp.content}]
            continue
        if resp.stop_reason == "refusal":
            raise BriefError(f"{model} declined the request (refusal)")
        if resp.stop_reason == "max_tokens":
            raise BriefError(f"{model} hit max_tokens before finishing")
        text = "\n".join(b.text for b in resp.content if b.type == "text")
        return text, searches, usage
    raise BriefError("server tool loop did not finish after 5 resumes")


def generate(conf: sc.Conference, phase: str, today: date, client=None) -> dict:
    """Research one brief. Returns {'brief', 'model', 'searches', 'usage'}."""
    if client is None:
        import anthropic
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise BriefError("ANTHROPIC_API_KEY not set")
        client = anthropic.Anthropic(api_key=key, timeout=900.0)
    prompt = build_prompt(conf, phase, today)
    model = MODEL
    try:
        text, searches, usage = _ask(client, model, prompt)
    except BriefError as e:
        if "refusal" not in str(e):
            raise
        model = REFUSAL_FALLBACK_MODEL
        text, searches, usage = _ask(client, model, prompt)
    # An answer is evidence of research only if a search ran (the 2026-11-13 Codex
    # lesson in this repo: 68 names "searched" by a model with no search tool).
    if searches == 0:
        raise BriefError("model answered without running a web search")
    brief = validate(_extract_json(text), phase)
    return {"brief": brief, "model": model, "searches": searches, "usage": usage}


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).replace("*", "").replace("`", "").strip()


def _http(url) -> str:
    u = str(url or "").strip()
    return u if re.match(r"^https?://[^\s<>|]+$", u) else ""


def validate(raw: dict, phase: str) -> dict:
    """Coerce the model's JSON to the contract. Raises when it carries no evidence."""
    def items(key: str, max_lines: int) -> list[dict]:
        out = []
        for it in (raw.get(key) or [])[:MAX_ITEMS]:
            if not isinstance(it, dict) or not _clean(it.get("title")):
                continue
            out.append({
                "title": _clean(it.get("title")),
                "tickers": [_clean(t).upper() for t in (it.get("tickers") or [])
                            if re.match(r"^[A-Za-z0-9.\-]{1,10}$", _clean(t))],
                "when": _clean(it.get("when")),
                "lines": [_clean(x) for x in (it.get("lines") or []) if _clean(x)][:max_lines],
                "url": _http(it.get("url")),
            })
        return out

    def strs(key: str, cap: int) -> list[str]:
        return [_clean(x) for x in (raw.get(key) or []) if _clean(x)][:cap]

    sources = [{"label": _clean(s.get("label")) or "source", "url": _http(s.get("url"))}
               for s in (raw.get("sources") or []) if isinstance(s, dict) and _http(s.get("url"))]
    if not sources:
        raise BriefError("brief cites no source URL")
    if phase == "pre":
        brief = {"significant": bool(raw.get("significant")),
                 "why_now": strs("why_now", 3),
                 "watch": items("watch", 3),
                 "sources": sources[:8], "caveat": _clean(raw.get("caveat"))}
        if brief["significant"] and not brief["watch"]:
            raise BriefError("pre-alert says significant but lists nothing to watch")
        return brief
    brief = {"summary": strs("summary", MAX_SUMMARY_BULLETS),
             "items": items("items", MAX_LINES_PER_ITEM),
             "expert_minds": strs("expert_minds", 4),
             "new_topics": strs("new_topics", 6),
             "fading_topics": strs("fading_topics", 6),
             "topics": strs("topics", 15),
             "sources": sources[:8], "caveat": _clean(raw.get("caveat"))}
    if not brief["summary"]:
        raise BriefError("post summary has no summary bullets")
    return brief


# ------------------------------------------------------------------------ render

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def fmt_dates(start: str, end: str) -> str:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if a == b:
        return f"{_MONTHS[a.month - 1]} {a.day}, {a.year}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{_MONTHS[a.month - 1]} {a.day}-{b.day}, {a.year}"
    if a.year == b.year:
        return f"{_MONTHS[a.month - 1]} {a.day} - {_MONTHS[b.month - 1]} {b.day}, {a.year}"
    return (f"{_MONTHS[a.month - 1]} {a.day}, {a.year} - "
            f"{_MONTHS[b.month - 1]} {b.day}, {b.year}")


def header_lines(conf: sc.Conference) -> list[str]:
    """Who the meeting is for and how big it is. JP: *"I am not going to know what AHA
    stands for"*, and *"if this is the big conference for this speciality or one of a
    number of them"*."""
    m = sc.meta_for(conf)
    out = [f"> *For*  {m.audience}",
           f"> *Standing*  {m.standing}"]
    if m.attendance:
        out.append(f"> *Size*  {m.attendance}")
    where = f" · {conf.location}" if conf.location else ""
    out.append(f"> *When*  {fmt_dates(conf.start, conf.end)}{where} · <{conf.url}|conference site>")
    return out


def _title(conf: sc.Conference, label: str) -> str:
    m = sc.meta_for(conf)
    return f"*{conf.short_name}* · {m.full_name} · _{label}_"


def _item_block(it: dict) -> list[str]:
    chips = " ".join(f"`{t}`" for t in it["tickers"])
    head = f"*{it['title']}*" + (f"  {chips}" if chips else "")
    out = [head]
    if it.get("when"):
        out.append(f"> _{it['when']}_")
    out += [f"> {ln}" for ln in it["lines"]]
    if it["url"]:
        out.append(f"> <{it['url']}|source>")
    return out


def _links(brief: dict) -> str:
    return " · ".join(f"<{s['url']}|{s['label']}>" for s in brief["sources"])


def render_post(conf: sc.Conference, brief: dict) -> str:
    L = [_title(conf, "post-conference summary"), *header_lines(conf), "",
         "*Summary*", *[f"• {b}" for b in brief["summary"]]]
    for it in brief["items"]:
        L += ["", *_item_block(it)]
    if brief["expert_minds"]:
        L += ["", "*What was on attendees' minds*", *[f"> {x}" for x in brief["expert_minds"]]]
    prior, _ = prior_topics(conf)
    vs = f" (vs {prior})" if prior else ""
    if brief["new_topics"]:
        L += ["", f"*New this year*{vs}", *[f"> {x}" for x in brief["new_topics"]]]
    if brief["fading_topics"]:
        L += ["", f"*No longer coming up*{vs}", *[f"> {x}" for x in brief["fading_topics"]]]
    if not (brief["new_topics"] or brief["fading_topics"]):
        L += ["", "_New vs. dropped topics: no grounded comparison with the prior meeting "
                  "was found._"]
    L += ["", f"*Links*  {_links(brief)}"]
    L.append("_Researched from the web by a model, not from the primary papers. "
             + (brief["caveat"] or "Check a figure before acting on it.") + "_")
    return "\n".join(L)


def render_pre(conf: sc.Conference, brief: dict, today: date) -> str:
    lead = (date.fromisoformat(conf.start) - today).days
    when = "starts today" if lead == 0 else f"starts in {lead} day{'s' if lead != 1 else ''}"
    L = [":rotating_light: " + _title(conf, f"pre-conference alert · {when}"),
         *header_lines(conf), ""]
    if brief["why_now"]:
        L += ["*Why it matters this year*", *[f"• {b}" for b in brief["why_now"]]]
    for it in brief["watch"]:
        L += ["", *_item_block(it)]
    L += ["", f"*Links*  {_links(brief)}"]
    L.append("_Researched from the web by a model ahead of the meeting. "
             + (brief["caveat"] or "Session times can move.") + "_")
    return "\n".join(L)


def channel_env(conf: sc.Conference) -> str:
    return (slack_bot.WALLST_CHANNEL_ENV if sc.channel_for(conf) == "wallst"
            else slack_bot.INDUSTRY_CHANNEL_ENV)


# --------------------------------------------------------------------------- run


def _save(conf: sc.Conference, phase: str, state: dict) -> None:
    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    p = state_path(conf, phase)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def run(today: Optional[date] = None, dry_run: bool = False, only: Optional[str] = None,
        phase: Optional[str] = None, client=None) -> dict:
    """Generate and post everything due. Returns a summary for the heartbeat.

    One brief failing does not stop the others: each is recorded in `errors` and the
    caller turns any error into a `partial` heartbeat.
    """
    today = today or date.today()
    due = due_briefs(today)
    if only:
        due = [d for d in due if d.conf.short_name.lower() == only.lower()]
    if phase:
        due = [d for d in due if d.phase == phase]
    deferred = due[MAX_BRIEFS_PER_RUN:]
    due = due[:MAX_BRIEFS_PER_RUN]
    summary = {"due": len(due), "posted": [], "quiet": [], "errors": [],
               "deferred": [f"{d.conf.short_name} {d.phase}" for d in deferred]}
    for d in due:
        label = f"{d.conf.short_name} {d.phase}"
        if dry_run:
            print(f"[dry-run] would research and post: {label} -> {sc.channel_for(d.conf)}")
            continue
        try:
            state = load_state(d.conf, d.phase)
            if not (state and state.get("brief")):
                got = generate(d.conf, d.phase, today, client=client)
                state = {"conference": d.conf.short_name, "series": d.conf.series,
                         "phase": d.phase, "generated_on": today.isoformat(), **got}
                _save(d.conf, d.phase, state)
                u = got["usage"]
                print(f"researched {label}: {got['searches']} searches, "
                      f"{u['input_tokens']} in / {u.get('cache_read_input_tokens', 0)} cache-read / "
                      f"{u['output_tokens']} out tokens ({got['model']})")
            brief = state["brief"]
            if d.phase == "pre" and not brief["significant"]:
                state["skipped_reason"] = "nothing significant expected"
                _save(d.conf, d.phase, state)
                summary["quiet"].append(label)
                continue
            text = (render_pre(d.conf, brief, today) if d.phase == "pre"
                    else render_post(d.conf, brief))
            env = channel_env(d.conf)
            if not slack_bot.configured(env):
                raise BriefError(f"{slack_bot.TOKEN_ENV} or {env} not set; brief is saved, "
                                 "not posted")
            state["posted_ts"] = slack_bot.post(
                env, text, f"{d.conf.short_name}: {d.phase}-conference "
                           f"{'alert' if d.phase == 'pre' else 'summary'}")
            state["posted_on"] = today.isoformat()
            _save(d.conf, d.phase, state)
            summary["posted"].append(label)
        except Exception as e:  # noqa: BLE001 - one brief must not stop the rest
            summary["errors"].append(f"{label}: {type(e).__name__}: {e}")
            print(f"ERROR {label}: {type(e).__name__}: {e}")
    return summary
