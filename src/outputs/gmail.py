"""Gmail output — weekly digest email.

Sends the Monday "forward 30/7" digest as an HTML email via the Gmail API,
mirroring daily-reads' `send_gmail_html`. Reuses the daily-reads OAuth token
(scopes include `gmail.send`) — NO fresh OAuth flow.

Auth (mirrors daily-reads/gmail_reader.get_gmail_service):
  CI    — GMAIL_OAUTH_JSON holds the full token JSON content as one env var.
  Local — GMAIL_OAUTH_JSON_PATH points at the token file on disk.

Recipient: EMAIL_TO (defaults to jroypeterson@gmail.com).

The digest email is best-effort relative to Slack: the caller decides whether
a send failure is fatal. This module raises on misconfiguration / send
failure so the caller can surface it (no silent drops).
"""
from __future__ import annotations

import base64
import json
import os
from email.mime.text import MIMEText

DEFAULT_EMAIL_TO = "jroypeterson@gmail.com"


def get_gmail_service():
    """Build a Gmail API service from the daily-reads OAuth token.

    CI path reads GMAIL_OAUTH_JSON (full JSON contents); local path reads the
    file at GMAIL_OAUTH_JSON_PATH. Refreshes the access token if expired.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_json = os.environ.get("GMAIL_OAUTH_JSON")
    if not token_json:
        path = os.environ.get("GMAIL_OAUTH_JSON_PATH")
        if path:
            with open(path, encoding="utf-8") as f:
                token_json = f.read()
    if not token_json:
        raise RuntimeError(
            "Gmail OAuth not configured. Set GMAIL_OAUTH_JSON (JSON contents, "
            "CI) or GMAIL_OAUTH_JSON_PATH (path to token JSON file, local)."
        )
    creds = Credentials.from_authorized_user_info(json.loads(token_json))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _recipient() -> str:
    return os.environ.get("EMAIL_TO", "").strip() or DEFAULT_EMAIL_TO


def send_html(subject: str, html: str, to: str | None = None) -> str:
    """Send an HTML email. Returns the Gmail message id. Raises on failure."""
    service = get_gmail_service()
    msg = MIMEText(html, "html")
    msg["to"] = to or _recipient()
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent.get("id", "")


def smoke_test() -> None:
    """Verify Gmail auth without sending — prints the authorized address."""
    service = get_gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    print(f"Gmail OK: authorized as {profile.get('emailAddress')!r}")
    print(f"  recipient (EMAIL_TO): {_recipient()}")
