"""The Mac Studio's weekly web-search pass: Codex on JP's ChatGPT subscription.

    python -m scripts.codex_web_search [--limit N] [--tickers A B] [--no-upload] [--out PATH]

JP 2026-11-13: "is there a way to get this websearch to happen through my subscription for
chatgpt? ... I can literally just type in the same query to the website and it will return
the answers." Measured that day: one `codex --search exec` call answered 12 companies in
37 seconds, 12 of 12 right, against USD 8-60 a month for the metered search APIs.

What it does, for the whole scan set (Coverage Manager's `analyst_days` scope):
  1. asks Codex, 12 companies a call, whether each has an investor / analyst / R&D /
     capital-markets day (the question is `web_answers.batch_prompt`);
  2. fetches every cited page ITSELF and cuts the snippet from that page's text
     (`web_answers.answer_to_hit`) -- nothing the model wrote reaches the snippet;
  3. writes `web_hits.json` and commits it to the repo through the GitHub API as
     `search_cache/web_hits.json`, where Monday's GitHub job reads it.

The file is written OUTSIDE Dropbox and uploaded by API on purpose: this checkout is a
Dropbox-synced working tree whose git directory lives on JRazerP, so a file written here
would arrive there as an untracked copy of a path the next `git pull` wants to create.

Subscription quota is finite and shared with the code-review loop (a ~6-day lockout on
2026-10-21). A usage-limit error stops the pass at once -- no retry, never buy credits --
and the names not reached are simply absent from the cache: the GitHub job searches those
through Perplexity and its heartbeat says so.

Exit: 0 = uploaded, with at most a few names not searched (GitHub searches those live);
1 = uploaded, but the quota stopped the pass or more than 5% of names were not searched;
2 = nothing usable, or the upload failed (the previous cache is left in place).
`run_guarded` posts an error heartbeat on any non-zero exit, so a single ticker one reply
skipped must not be one: an alarm lit every week is an alarm nobody reads.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.discovery import web_answers                          # noqa: E402
from src.discovery.web_source import CACHE_SCHEMA               # noqa: E402

GH_REPO = "jroypeterson/analyst-days"
GH_PATH = "search_cache/web_hits.json"
GH_BRANCH = "master"
BATCH_SIZE = 12
BATCH_TIMEOUT_S = 600
EFFORT = "low"
QUOTA_MARKERS = ("usage limit", "hit your usage", "quota")


class QuotaExhausted(RuntimeError):
    """The ChatGPT subscription refused: stop the pass, do not retry."""


class BatchFailed(RuntimeError):
    """One Codex call produced no usable answer."""


class SearchUnavailable(RuntimeError):
    """Codex answered WITHOUT running a single web search: stop the pass.

    ⛑ 2026-11-13, the first full pass on the Mac. Its Codex install had no
    `codex-code-mode-host` beside the binary, so the web-search tool could not start. Codex
    still replied -- one batch with twelve tidy nulls, others with an error object -- and
    this script recorded 68 names as "searched, no event" and uploaded that. An answer is
    evidence of a search only if a search ran, and that is counted from the event stream,
    never taken from the reply.
    """


def _codex_bin() -> str:
    # Windows: `codex` on PATH is a shell script CreateProcess cannot run; the .cmd shim can.
    first, second = ("codex.cmd", "codex") if sys.platform == "win32" else ("codex", "codex.cmd")
    exe = os.environ.get("CODEX_BIN") or shutil.which(first) or shutil.which(second)
    if not exe:
        raise SystemExit("codex executable not found on PATH (set CODEX_BIN)")
    return exe


def read_events(stdout: str) -> tuple[str, dict, list[str], int]:
    """(final message, token usage, error texts, web searches run) from `codex exec --json`."""
    final, usage, errors, searches = "", {}, [], 0
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        item = ev.get("item") or {}
        if ev.get("type") == "error" or item.get("type") == "error":
            errors.append(str(ev.get("message") or item.get("message") or ev)[:400])
        if ev.get("type") == "turn.failed":
            errors.append(str((ev.get("error") or {}).get("message") or ev)[:400])
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            final = item.get("text") or final
        if ev.get("type") == "item.completed" and item.get("type") == "web_search":
            searches += 1
        if ev.get("type") == "turn.completed":
            usage = ev.get("usage") or usage
    return final, usage, errors, searches


def ask_codex(prompt: str, *, runner=subprocess.run, workdir: str | None = None) -> tuple[str, dict]:
    """(final message, usage) for one `codex --search exec` call with the prompt on stdin.

    Raises QuotaExhausted, SearchUnavailable (both mean: stop asking) or BatchFailed
    (retry once). Shared by the weekly pass and the history backfill (`src/backfill_web.py`)
    so both refuse an answer that has no web search behind it.
    """
    try:
        p = runner(
            [_codex_bin(), "--search", "exec", "--skip-git-repo-check", "--ephemeral",
             "-s", "read-only", "-c", f'model_reasoning_effort="{EFFORT}"', "--json", "-"],
            input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=BATCH_TIMEOUT_S, cwd=workdir)
    except subprocess.TimeoutExpired as e:
        raise BatchFailed(f"codex timed out after {BATCH_TIMEOUT_S}s") from e
    final, usage, errors, searches = read_events(p.stdout or "")
    blob = " ".join(errors + [p.stderr or ""]).lower()
    if any(m in blob for m in QUOTA_MARKERS):
        raise QuotaExhausted((errors or [p.stderr or ""])[0][:300])
    if not final:
        raise BatchFailed("no final message (exit %s): %s" % (
            p.returncode, (errors or [(p.stderr or "")[-300:]])[0]))
    if searches == 0:
        raise SearchUnavailable(
            "Codex replied without running a web search (is the web-search tool installed? "
            "stderr: %s)" % ((p.stderr or "").strip().splitlines() or ["-"])[-1][:200])
    return final, usage


def run_batch(companies: list[tuple[str, str]], today: date, *, runner=subprocess.run,
              workdir: str | None = None) -> tuple[dict, dict]:
    """({ticker: Answer|None}, usage) for one Codex call.

    Raises QuotaExhausted, SearchUnavailable (both stop the pass) or BatchFailed (retried once)."""
    final, usage = ask_codex(web_answers.batch_prompt(companies, today), runner=runner,
                             workdir=workdir)
    try:
        answers = web_answers.parse_answers(final, [t for t, _ in companies])
    except web_answers.AnswerParseError as e:
        raise BatchFailed(str(e)) from e
    return answers, usage


def search_all(universe: list, today: date, *, run=run_batch, sleep=time.sleep) -> dict:
    """Run every batch. Returns {"answers", "failed", "usage", "quota"}."""
    answers: dict = {}
    failed: dict[str, str] = {}
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "calls": 0}
    quota = ""
    names = {t.ticker: t.company_name for t in universe}
    batches = [list(names.items())[i:i + BATCH_SIZE] for i in range(0, len(names), BATCH_SIZE)]
    with tempfile.TemporaryDirectory(prefix="codex-search-") as workdir:
        for n, batch in enumerate(batches, 1):
            if quota:
                failed.update({t: "not reached: " + quota for t, _ in batch})
                continue
            got: dict = {}
            for attempt in (1, 2):
                try:
                    got, u = run(batch, today, workdir=workdir)
                    usage["calls"] += 1
                    for k in ("input_tokens", "cached_input_tokens", "output_tokens"):
                        usage[k] += int(u.get(k) or 0)
                    break
                except QuotaExhausted as e:
                    quota = f"ChatGPT subscription usage limit ({e})"
                    break
                except SearchUnavailable as e:
                    quota = str(e)
                    break
                except BatchFailed as e:
                    reason = str(e)
                    if attempt == 1:
                        sleep(5)
                    else:
                        failed.update({t: "batch failed twice: " + reason[:160] for t, _ in batch})
            if quota:
                failed.update({t: "not reached: " + quota for t, _ in batch})
                continue
            answers.update(got)
            for t, _ in batch:                 # asked, and the reply left it out
                if t not in got:
                    failed.setdefault(t, "the reply did not cover this ticker")
            print(f"[codex] batch {n}/{len(batches)}: {len(got)}/{len(batch)} answered, "
                  f"{sum(1 for v in got.values() if v)} event(s)", flush=True)
    return {"answers": answers, "failed": failed, "usage": usage, "quota": quota}


def build_cache(universe: list, found: dict, *, now: datetime, fetch=None) -> dict:
    names = {t.ticker: t.company_name for t in universe}
    results: dict[str, dict] = {}
    kw = {"fetch": fetch} if fetch else {}
    for ticker, answer in found["answers"].items():
        if answer is None:
            results[ticker] = {"hit": None}
            continue
        hit = web_answers.answer_to_hit(ticker, names[ticker], answer, **kw)
        results[ticker] = {"hit": hit, "claimed_date": answer.date,
                           "claimed_type": answer.event_type}
        print(f"[verify] {ticker:8} {answer.date or 'no date':10} "
              f"{'read' if hit['verified'] else 'UNREADABLE':10} "
              f"{'date on page' if hit['date_on_page'] else 'date NOT on page':16} "
              f"{answer.source_url[:90]}", flush=True)
    return {
        "schema_version": CACHE_SCHEMA,
        "provider": "codex",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": socket.gethostname(),
        "scan_set_count": len(universe),
        "searched_count": len(results),
        "failed": found["failed"],
        "quota_stop": found["quota"],
        "usage": found["usage"],
        "results": results,
    }


def upload(path: Path, *, runner=subprocess.run) -> None:
    """Commit the file to the repo through the GitHub contents API (needs `gh` logged in)."""
    api = f"repos/{GH_REPO}/contents/{GH_PATH}"
    cur = runner(["gh", "api", f"{api}?ref={GH_BRANCH}", "--jq", ".sha"],
                 capture_output=True, text=True)
    body = {"message": "search: refresh the Mac's Codex web-search cache",
            "branch": GH_BRANCH,
            "content": base64.b64encode(path.read_bytes()).decode("ascii")}
    if cur.returncode == 0 and cur.stdout.strip():
        body["sha"] = cur.stdout.strip()
    put = runner(["gh", "api", "-X", "PUT", api, "--input", "-"],
                 input=json.dumps(body), capture_output=True, text=True)
    if put.returncode != 0:
        raise RuntimeError("GitHub upload failed: " + (put.stderr or put.stdout)[-400:])


MISSED_OK_SHARE = 0.05


def exit_code(cache: dict) -> int:
    """0 or 1 for an uploaded cache -- see the module docstring."""
    missed = len(cache["failed"])
    if cache["quota_stop"] or missed > MISSED_OK_SHARE * max(1, cache["scan_set_count"]):
        return 1
    return 0


def default_out() -> Path:
    base = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache")
    return base / "fleet" / "analyst-days" / "web_hits.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tickers", nargs="*")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    os.environ.setdefault("COVERAGE_MANAGER_PATH", str(REPO.parent / "Coverage Manager"))
    from src.universe import load_scan_universe
    universe = load_scan_universe()
    partial_request = bool(args.tickers or args.limit)
    if args.tickers:
        wanted = {t.upper() for t in args.tickers}
        universe = [t for t in universe if t.ticker.upper() in wanted]
    if args.limit:
        universe = universe[:args.limit]
    print(f"[codex] scan set {len(universe)} name(s), {BATCH_SIZE} a call", flush=True)

    t0 = time.time()
    found = search_all(universe, date.today())
    cache = build_cache(universe, found, now=datetime.now(timezone.utc))
    out = args.out or default_out()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cache, indent=1) + "\n", encoding="utf-8")

    events = [r for r in cache["results"].values() if r.get("hit")]
    print(f"[codex] searched {cache['searched_count']}/{len(universe)} in {time.time() - t0:.0f}s; "
          f"{len(events)} event(s), {sum(1 for r in events if r['hit']['verified'])} source page(s) read, "
          f"{sum(1 for r in events if r['hit']['date_on_page'])} with the date on the page; "
          f"{len(cache['failed'])} not searched; usage {cache['usage']}", flush=True)
    if cache["quota_stop"]:
        print("[codex] STOPPED: " + cache["quota_stop"], flush=True)
    for t, why in list(cache["failed"].items())[:8]:
        print(f"[codex] not searched: {t}: {why}", flush=True)
    print(f"[codex] wrote {out}", flush=True)

    if not cache["results"]:
        print("[codex] nothing searched - the previous cache is left in place", flush=True)
        return 2
    if args.no_upload or partial_request:
        print("[codex] not uploaded (%s)" % ("--no-upload" if args.no_upload
                                             else "a --tickers / --limit run is not the whole scan set"))
        return 0
    try:
        upload(out)
    except Exception as e:                                        # noqa: BLE001
        print(f"[codex] {e}", flush=True)
        return 2
    print(f"[codex] uploaded to {GH_REPO}:{GH_PATH}", flush=True)
    return exit_code(cache)


if __name__ == "__main__":
    raise SystemExit(main())
