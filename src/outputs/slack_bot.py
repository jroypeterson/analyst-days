"""Post to Slack with the ClaudeBot token (chat.postMessage), not a webhook.

The conference channels use the bot token because a webhook is locked to one
channel and has to be minted by hand: `SLACK_WEBHOOK_CONFERENCES` sat unset from
2026-08-12 to 2026-11-17 and the calendar post was skipped every week (board
#333). The bot token already exists, reaches any public channel
(`chat:write.public`), and returns a `ts`, so delivery is proven by Slack's `ok`
field rather than an HTTP status.

Channel ids come from the environment, never from this file: the repo is public.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_ENV = "SLACK_BOT_TOKEN"
INDUSTRY_CHANNEL_ENV = "SLACK_CONFERENCES_INDUSTRY_CHANNEL_ID"
WALLST_CHANNEL_ENV = "SLACK_CONFERENCES_WALLST_CHANNEL_ID"

_SECTION_CAP = 2800  # Slack's limit is 3000 per section text
# Slack's read methods ignore a JSON body ("missing required field: channel",
# measured 2026-11-17); they take form-encoded arguments.
_FORM_METHODS = frozenset({"pins.list", "bookmarks.list", "auth.test"})


class SlackBotError(RuntimeError):
    pass


def configured(channel_env: str) -> bool:
    return bool(os.environ.get(TOKEN_ENV, "").strip()
                and os.environ.get(channel_env, "").strip())


def split_sections(text: str, cap: int = _SECTION_CAP) -> list[str]:
    """Split mrkdwn at line boundaries so no section exceeds `cap`. Never truncates."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > cap:           # a single over-long line: hard-wrap it
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:cap])
            line = line[cap:]
        if cur and len(cur) + 1 + len(line) > cap:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur.strip():
        chunks.append(cur)
    return chunks


def blocks_for(text: str) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": c}}
            for c in split_sections(text) if c.strip()]


def call(method: str, payload: dict) -> dict:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise SlackBotError(f"{TOKEN_ENV} not set")
    last = ""
    for attempt in range(4):
        if method in _FORM_METHODS:
            data = urllib.parse.urlencode(payload).encode()
            ctype = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode()
            ctype = "application/json; charset=utf-8"
        req = urllib.request.Request(
            f"https://slack.com/api/{method}", data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": ctype},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(int(e.headers.get("Retry-After", "5")) + attempt * 5)
                last = f"HTTP {e.code}"
                continue
            raise SlackBotError(f"{method}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)
            time.sleep(5 + attempt * 10)
            continue
        if body.get("ok"):
            return body
        if body.get("error") == "ratelimited":
            time.sleep(10 + attempt * 10)
            last = "ratelimited"
            continue
        raise SlackBotError(f"{method}: {body.get('error')} "
                            f"{body.get('response_metadata', '')}")
    raise SlackBotError(f"{method}: gave up after retries ({last})")


def post(channel_env: str, text: str, fallback: str) -> str:
    """Post `text` (mrkdwn) to the channel named by `channel_env`. Returns the ts.

    More than 50 blocks is split into continuation messages rather than cut.
    """
    channel = os.environ.get(channel_env, "").strip()
    if not channel:
        raise SlackBotError(f"{channel_env} not set")
    blocks = blocks_for(text)
    first_ts = ""
    for i in range(0, len(blocks), 45):
        body = call("chat.postMessage", {
            "channel": channel, "text": fallback, "blocks": blocks[i:i + 45],
            "unfurl_links": False, "unfurl_media": False})
        first_ts = first_ts or body["ts"]
    return first_ts
