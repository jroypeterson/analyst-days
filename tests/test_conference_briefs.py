"""Conference briefs, the two-channel look-ahead, and the per-series facts.

No test here reaches Slack or the Anthropic API: every one runs against a fake client
or a monkeypatched poster, and the autouse fixture strips the real credentials so a
mistake fails closed instead of posting to JP's channels.
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from src import conference_briefs as briefs
from src import conference_calendar as cal
from src import seed_conferences as sc
from src.outputs import slack_bot
from src.state.schema import init_db


@pytest.fixture(autouse=True)
def _sealed(monkeypatch, tmp_path):
    for k in ("SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY", slack_bot.INDUSTRY_CHANNEL_ENV,
              slack_bot.WALLST_CHANNEL_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(briefs, "BRIEF_DIR", tmp_path / "briefs")

    def _no_network(*a, **k):
        raise AssertionError("test reached the Slack API")

    monkeypatch.setattr(slack_bot, "call", _no_network)


def _conf(short="Test 2026", series="aha", start="2026-11-06", end="2026-11-09",
          kind=sc.SCIENTIFIC):
    return sc.C(f"{short} full", short, series, start, end, "Chicago, IL",
                "https://example.org/meeting", "verified", "2026-11-17", "why", kind=kind)


POST_RAW = {
    "summary": ["one", "two", "three", "four", "five"],
    "items": [{"title": "ZEUS, ziltivekimab (Novo Nordisk)", "tickers": ["nvo", "not a ticker!"],
               "lines": ["MACE hazard ratio 0.99, 95% CI 0.88-1.11, n=6,376"],
               "url": "https://example.org/zeus"}],
    "expert_minds": ["Agentic AI, per the conference chair"],
    "new_topics": ["gene editing"], "fading_topics": [],
    "topics": ["lp(a)", "il-6"],
    "sources": [{"label": "newsroom", "url": "https://example.org/news"},
                {"label": "bad", "url": "javascript:alert(1)"}],
    "caveat": "",
}


# ----------------------------------------------------------------- series facts


def test_every_series_has_meta_and_every_field_a_reader_prints():
    """JP: 'I am not going to know what AHA stands for'. A header that renders
    'None' is the failure, so a series without its facts fails the build."""
    for c in sc.CONFERENCES:
        m = sc.meta_for(c)          # KeyError here = a series with no entry
        for field in ("full_name", "organizer", "audience", "standing", "category"):
            assert getattr(m, field), f"{c.series}: empty {field}"


def test_circuit_instances_are_dated_unique_and_carry_a_source():
    data = json.loads(open(sc.CIRCUIT_PATH, encoding="utf-8").read())
    names = [c.name for c in sc.CONFERENCES]
    shorts = [c.short_name for c in sc.CONFERENCES]
    assert len(names) == len(set(names)), "conferences.name is UNIQUE in the table"
    assert len(shorts) == len(set(shorts)), "short_name is the brief-file key"
    for i in data["instances"]:
        assert i["start"] <= i["end"], i["name"]
        assert i["series"] in data["series"], i["name"]
        assert i["url"].startswith("http"), i["name"]


def test_bank_conferences_go_to_wall_street_everything_else_to_industry():
    by_series = {c.series: c for c in sc.CONFERENCES}
    assert sc.channel_for(by_series["jpm-healthcare"]) == "wallst"
    assert sc.channel_for(by_series["piper-hc"]) == "wallst"
    assert sc.channel_for(by_series["aha"]) == "industry"
    assert sc.channel_for(by_series["ahca-ncal"]) == "industry"     # nursing facilities
    assert sc.channel_for(by_series["adlm"]) == "industry"          # labs
    assert sc.channel_for(by_series["nacds-annual"]) == "industry"  # pharmacies


def test_the_main_street_groups_jp_named_are_on_the_list():
    cats = {m.category for m in sc.SERIES_META.values()}
    for wanted in ("Nursing facilities & senior living", "Labs & diagnostics",
                   "Pharmacy & distribution", "Bank conferences"):
        assert wanted in cats


# -------------------------------------------------------------------------- due


def test_post_summary_is_due_2_to_21_days_after_the_end_and_only_then():
    c = _conf(end="2026-11-09")
    due = lambda d: [(x.conf.short_name, x.phase) for x in briefs.due_briefs(d, [c])]
    assert due(date(2026, 11, 10)) == []                      # 1 day: too soon
    assert due(date(2026, 11, 11)) == [("Test 2026", "post")]  # 2 days
    assert due(date(2026, 11, 30)) == [("Test 2026", "post")]  # 21 days
    assert due(date(2026, 12, 1)) == []                       # 22 days: no backfill


def test_pre_alert_is_due_in_the_week_before_a_scientific_meeting_only():
    sci = _conf(start="2026-12-12", end="2026-12-15")
    trade = _conf(short="Trade 2026", series="himss", start="2026-12-12",
                  end="2026-12-15", kind=sc.TRADE_SHOW)
    got = [(x.conf.short_name, x.phase)
           for x in briefs.due_briefs(date(2026, 12, 6), [sci, trade])]
    assert got == [("Test 2026", "pre")]
    assert briefs.due_briefs(date(2026, 12, 4), [sci]) == []   # 8 days out


def test_bank_conferences_get_no_summary_unless_the_series_opts_in():
    piper = _conf(short="Piper 2026", series="piper-hc", kind=sc.INVESTOR)
    jpm = _conf(short="JPM x", series="jpm-healthcare", kind=sc.INVESTOR)
    got = [x.conf.short_name for x in briefs.due_briefs(date(2026, 11, 12), [piper, jpm])]
    assert got == ["JPM x"]


def test_a_posted_brief_is_never_due_again(tmp_path):
    c = _conf()
    briefs._save(c, "post", {"brief": {"summary": ["x"]}, "posted_ts": "1.0"})
    assert briefs.due_briefs(date(2026, 11, 12), [c]) == []


# --------------------------------------------------------------------- validate


def test_validate_caps_the_summary_at_four_and_drops_unsafe_links():
    b = briefs.validate(dict(POST_RAW), "post")
    assert len(b["summary"]) == 4                       # JP: "a 4 line at most summary"
    assert [s["url"] for s in b["sources"]] == ["https://example.org/news"]
    assert b["items"][0]["tickers"] == ["NVO"]          # 'not a ticker!' is not a chip


def test_a_brief_with_no_source_is_refused():
    raw = dict(POST_RAW, sources=[])
    with pytest.raises(briefs.BriefError, match="no source"):
        briefs.validate(raw, "post")


def test_markup_from_the_model_cannot_break_the_layout():
    raw = dict(POST_RAW, summary=["*bold* and `code`\nsecond line"])
    assert briefs.validate(raw, "post")["summary"] == ["bold and code second line"]


# ----------------------------------------------------------------------- render


def test_post_render_carries_who_it_is_for_standing_and_the_conference_link():
    c = next(x for x in sc.CONFERENCES if x.short_name == "AHA 2026")
    text = briefs.render_post(c, briefs.validate(dict(POST_RAW), "post"))
    assert "American Heart Association" in text          # what AHA stands for
    assert "> *For*  Heart doctors" in text
    assert "> *Standing*  1 of 3 primary global cardiology meetings" in text
    assert "> *Size*" in text
    assert "<https://professional.heart.org/en/meetings/scientific-sessions|conference site>" in text
    assert text.count("\n• ") == 4
    assert "> MACE hazard ratio 0.99, 95% CI 0.88-1.11, n=6,376" in text   # the number
    assert "`NVO`" in text


# -------------------------------------------------------------------- generation


def _fake_client(text, searches=1, stop="end_turn"):
    content = [SimpleNamespace(type="server_tool_use", name="web_search")] * searches
    content = content + [SimpleNamespace(type="text", text=text)]
    msg = SimpleNamespace(content=content, stop_reason=stop,
                          usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                                cache_read_input_tokens=0,
                                                cache_creation_input_tokens=0))

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return msg

    calls = []

    def stream(**kw):
        calls.append(kw)
        return _Stream()

    return SimpleNamespace(messages=SimpleNamespace(stream=stream), calls=calls)


def test_an_answer_with_no_web_search_behind_it_is_refused():
    client = _fake_client("```json\n" + json.dumps(POST_RAW) + "\n```", searches=0)
    with pytest.raises(briefs.BriefError, match="without running a web search"):
        briefs.generate(_conf(), "post", date(2026, 11, 12), client=client)


def test_generate_parses_the_fenced_json_and_asks_for_evidence():
    client = _fake_client("notes\n```json\n" + json.dumps(POST_RAW) + "\n```")
    got = briefs.generate(_conf(), "post", date(2026, 11, 12), client=client)
    assert got["searches"] == 1 and len(got["brief"]["summary"]) == 4
    prompt = client.calls[0]["messages"][0]["content"]
    assert "confidence interval or\np-value" in prompt or "p-value" in prompt
    assert "Today is 2026-11-12" in prompt


# -------------------------------------------------------------------------- run


def _configure(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv(slack_bot.INDUSTRY_CHANNEL_ENV, "C_IND")
    monkeypatch.setenv(slack_bot.WALLST_CHANNEL_ENV, "C_WALL")


def test_run_posts_once_and_a_failed_post_does_not_pay_for_research_twice(monkeypatch):
    _configure(monkeypatch)
    c = _conf()
    monkeypatch.setattr(sc, "CONFERENCES", [c])
    made = []

    def fake_generate(conf, phase, today, client=None):
        made.append(conf.short_name)
        return {"brief": briefs.validate(dict(POST_RAW), "post"), "model": "m",
                "searches": 3, "usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(briefs, "generate", fake_generate)

    def boom(env, text, fallback):
        raise slack_bot.SlackBotError("channel_not_found")

    monkeypatch.setattr(slack_bot, "post", boom)
    s1 = briefs.run(today=date(2026, 11, 12))
    assert s1["posted"] == [] and len(s1["errors"]) == 1

    posted = []
    monkeypatch.setattr(slack_bot, "post",
                        lambda env, text, fallback: posted.append(env) or "9.9")
    s2 = briefs.run(today=date(2026, 11, 13))
    assert s2["posted"] == ["Test 2026 post"]
    assert posted == [slack_bot.INDUSTRY_CHANNEL_ENV]
    assert made == ["Test 2026"], "the saved brief must be reused, not researched again"

    s3 = briefs.run(today=date(2026, 11, 14))
    assert s3["due"] == 0 and len(posted) == 1


def test_a_quiet_pre_alert_is_recorded_and_not_posted(monkeypatch):
    _configure(monkeypatch)
    c = _conf(start="2026-12-12", end="2026-12-15")
    monkeypatch.setattr(sc, "CONFERENCES", [c])
    quiet = {"significant": False, "why_now": [], "watch": [],
             "sources": [{"label": "program", "url": "https://example.org/p"}], "caveat": ""}
    monkeypatch.setattr(briefs, "generate", lambda *a, **k: {
        "brief": briefs.validate(quiet, "pre"), "model": "m", "searches": 2,
        "usage": {"input_tokens": 1, "output_tokens": 1}})
    monkeypatch.setattr(slack_bot, "post", lambda *a: pytest.fail("posted a quiet alert"))
    s = briefs.run(today=date(2026, 12, 7))
    assert s["quiet"] == ["Test 2026 pre"] and s["posted"] == []
    assert briefs.run(today=date(2026, 12, 8))["due"] == 0


def test_the_spend_guard_defers_and_names_what_it_left(monkeypatch):
    confs = [_conf(short=f"C{i} 2026", series="aha") for i in range(7)]
    monkeypatch.setattr(sc, "CONFERENCES", confs)
    s = briefs.run(today=date(2026, 11, 12), dry_run=True)
    assert s["due"] == briefs.MAX_BRIEFS_PER_RUN
    assert len(s["deferred"]) == 2 and s["deferred"][0].startswith("C")


# -------------------------------------------------------------------- look-ahead


def test_presenters_need_the_conference_named_not_just_an_overlapping_date(tmp_path):
    """Piper Sandler, Citi and Evercore share the first week of December."""
    conn = init_db(tmp_path / "events.db")
    piper = next(c for c in sc.CONFERENCES if c.short_name == "Piper Sandler Healthcare 2026")
    now = "2026-11-17T00:00:00+00:00"

    def add(ticker, excerpt, status="confirmed"):
        cur = conn.execute(
            "INSERT INTO events (ticker, event_type, start_date, status, first_seen_at, "
            "last_seen_at) VALUES (?, 'conference', ?, ?, ?, ?)",
            (ticker, piper.start, status, now, now))
        conn.execute(
            "INSERT INTO event_sources (event_id, source_type, source_url, source_excerpt, "
            "retrieved_at) VALUES (?, 'TAVILY_HIT', 'https://x', ?, ?)",
            (cur.lastrowid, excerpt, now))

    add("ABT", "Abbott will present at the Piper Sandler 38th Annual Healthcare Conference")
    add("LLY", "Lilly to participate in the Citi Global Healthcare Conference")
    add("BSX", "Boston Scientific at the Piper Sandler conference", status="cancelled")
    add("UNH", "UnitedHealth hosts a Twin Cities investor day")   # 'Citi' inside 'Cities'
    conn.commit()
    assert cal.presenters(conn, piper) == ["ABT"]
    citi = next(c for c in sc.CONFERENCES if c.short_name == "Citi Healthcare 2026")
    assert cal.presenters(conn, citi) == ["LLY"]
    conn.close()


def test_look_ahead_names_series_with_no_published_date_instead_of_guessing():
    text, _ = cal.build_weekly("wallst", date(2026, 11, 17))
    assert "Next date not published yet" in text
    assert "Goldman Sachs Annual Global Healthcare Conference" in text
    assert "Piper Sandler Annual Healthcare Conference" in text.split("Next date not published")[0]


def test_look_ahead_counts_what_it_does_not_list():
    text, n = cal.build_weekly("industry", date(2026, 11, 17))
    assert n > cal.LATER_LINES
    assert "more after" in text and "in the inventory" in text


def test_pinned_overview_is_bullets():
    """SLACK_STYLE.md section 6: a pinned card is bullets under bold labels."""
    for ch in ("industry", "wallst"):
        lines = cal.overview_text(ch).split("\n")
        body = lines[1:-1]
        assert all(ln.startswith("• ") or (ln.startswith("*") and ln.endswith("*"))
                   for ln in body), [ln for ln in body if not ln.startswith(("• ", "*"))]


def test_long_text_is_split_not_truncated():
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    parts = slack_bot.split_sections(text)
    assert all(len(p) <= 2800 for p in parts)
    assert "\n".join(parts) == text
