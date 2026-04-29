"""TickTick output — "Analyst Days" list.

One task per pushable event. Title format: `[Investor Day] AFRM` (event-type
label in brackets + ticker). Due date = event start_date. Description holds
company name + source URL + multi-day flag.

Mirrors Slack/Calendar policy via PUSHABLE_EVENT_TYPES — conferences are
not pushed here.

Auth: TICKTICK_ACCESS_TOKEN — same token earnings_agent uses (180-day
rotation; will 401 when expired).

API:
  GET  /open/v1/project                  list projects
  POST /open/v1/project                  create project
  POST /open/v1/task                     create task
  GET  /open/v1/task/{projectId}/{id}    get task
  POST /open/v1/task/{id}                update task
  DELETE /open/v1/project/{pid}/task/{id}  delete task
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Optional

import requests

from src.state.events_repo import is_pushable

logger = logging.getLogger("analyst_days.ticktick")

API_BASE = "https://api.ticktick.com/open/v1"
LIST_NAME = "Analyst Days"
# Same parent group earnings_agent uses, so the list shows under the
# existing TickTick group in the UI.
DEFAULT_GROUP_ID = "6887b7f873800767fff51bf5"

EVENT_TYPE_LABELS = {
    "investor_day": "Investor Day",
    "analyst_day": "Analyst Day",
    "rd_day": "R&D Day",
    "capital_markets_day": "Capital Markets Day",
    "conference": "Conference",
}


class TickTickError(Exception):
    pass


class TickTickTokenExpired(TickTickError):
    pass


def _token() -> str:
    t = os.environ.get("TICKTICK_ACCESS_TOKEN", "").strip()
    if not t:
        raise TickTickError("TICKTICK_ACCESS_TOKEN not set")
    return t


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


def _check(resp: requests.Response, where: str) -> None:
    if resp.status_code == 401:
        raise TickTickTokenExpired(f"{where}: token expired (401)")
    if resp.status_code >= 400:
        raise TickTickError(
            f"{where}: HTTP {resp.status_code} — {resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# List management
# ---------------------------------------------------------------------------


def find_or_create_list(list_name: str = LIST_NAME) -> str:
    """Return the project ID for the analyst-days list, creating it if needed."""
    resp = requests.get(f"{API_BASE}/project", headers=_headers(), timeout=15)
    _check(resp, "list projects")
    for p in resp.json():
        if p.get("name") == list_name:
            logger.info("Found TickTick list %r (id=%s)", list_name, p["id"])
            return p["id"]

    payload = {"name": list_name, "groupId": DEFAULT_GROUP_ID}
    resp = requests.post(
        f"{API_BASE}/project", headers=_headers(), json=payload, timeout=15
    )
    _check(resp, "create project")
    project = resp.json()
    pid = project.get("id")
    if not pid:
        raise TickTickError(f"create project returned no id: {project!r}")
    logger.info("Created TickTick list %r (id=%s)", list_name, pid)
    return pid


# ---------------------------------------------------------------------------
# Task content builders
# ---------------------------------------------------------------------------


def _task_title(event_row) -> str:
    label = EVENT_TYPE_LABELS.get(event_row["event_type"], event_row["event_type"])
    return f"[{label}] {event_row['ticker']}"


def _task_content(event_row, source_url: Optional[str], rationale: Optional[str]) -> str:
    parts: list[str] = []
    if event_row["company_name"]:
        parts.append(event_row["company_name"])
    if event_row["multi_day"] and event_row["end_date"]:
        parts.append(
            f"Multi-day event: {event_row['start_date']} – {event_row['end_date']}"
        )
    parts.append(f"Confidence: {event_row['confidence']:.2f}")
    if source_url:
        parts.append(f"Source: {source_url}")
    if rationale:
        parts.append("")
        parts.append(rationale)
    return "\n".join(parts)


def _due_date_iso(start_date_iso: str) -> str:
    """TickTick wants ISO 8601 with timezone. 09:00 UTC = 5am ET / 4am ET (DST),
    early enough that the task shows under the right calendar day in TickTick.
    """
    return f"{start_date_iso}T09:00:00.000+0000"


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


def create_task(
    list_id: str,
    title: str,
    content: str,
    due_date: str,
) -> str:
    payload = {
        "title": title,
        "content": content,
        "dueDate": _due_date_iso(due_date),
        "projectId": list_id,
    }
    resp = requests.post(
        f"{API_BASE}/task", headers=_headers(), json=payload, timeout=15
    )
    _check(resp, f"create task {title!r}")
    task = resp.json()
    task_id = task.get("id")
    if not task_id:
        raise TickTickError(f"create task returned no id: {task!r}")
    return task_id


def update_task(
    list_id: str,
    task_id: str,
    title: str,
    content: str,
    due_date: str,
) -> None:
    """Refresh title / content / due date on an existing task. Idempotent."""
    resp = requests.get(
        f"{API_BASE}/task/{list_id}/{task_id}", headers=_headers(), timeout=15
    )
    if resp.status_code == 404:
        # Task was deleted manually — caller can retry creation.
        raise TickTickError(f"task {task_id} not found (deleted?)")
    _check(resp, f"get task {task_id}")
    data = resp.json()
    data["title"] = title
    data["content"] = content
    data["dueDate"] = _due_date_iso(due_date)

    resp = requests.post(
        f"{API_BASE}/task/{task_id}", headers=_headers(), json=data, timeout=15
    )
    _check(resp, f"update task {task_id}")


def delete_task(list_id: str, task_id: str) -> bool:
    resp = requests.delete(
        f"{API_BASE}/project/{list_id}/task/{task_id}",
        headers=_headers(),
        timeout=15,
    )
    if resp.status_code in (200, 204):
        return True
    if resp.status_code == 404:
        return False  # already gone
    _check(resp, f"delete task {task_id}")
    return False


# ---------------------------------------------------------------------------
# High-level upsert (for fan-out)
# ---------------------------------------------------------------------------


def upsert_event_task(
    conn: sqlite3.Connection,
    event_row,
    list_id: str,
) -> str:
    """Create or update the TickTick task for one event row.

    Returns the task ID. Persists it on events.ticktick_task_id.
    Raises if event_type is non-pushable (defense in depth).
    """
    if not is_pushable(event_row["event_type"]):
        raise ValueError(
            f"Refusing to push non-pushable event_type={event_row['event_type']!r} "
            "to TickTick"
        )
    if not event_row["start_date"]:
        raise ValueError("Cannot push imprecise event to TickTick — start_date is null")

    src = conn.execute(
        "SELECT source_url, source_excerpt FROM event_sources "
        "WHERE event_id = ? ORDER BY id ASC LIMIT 1",
        (event_row["id"],),
    ).fetchone()
    source_url = src["source_url"] if src else None
    rationale = src["source_excerpt"] if src else None

    title = _task_title(event_row)
    content = _task_content(event_row, source_url, rationale)
    due_date = event_row["start_date"]

    existing_id = event_row["ticktick_task_id"]
    if existing_id:
        try:
            update_task(list_id, existing_id, title, content, due_date)
            logger.info("ticktick updated event_id=%s task_id=%s",
                        event_row["id"], existing_id)
            return existing_id
        except TickTickError as e:
            logger.warning(
                "ticktick update failed for event_id=%s task_id=%s; re-creating: %s",
                event_row["id"], existing_id, e,
            )

    new_id = create_task(list_id, title, content, due_date)
    conn.execute(
        "UPDATE events SET ticktick_task_id = ? WHERE id = ?",
        (new_id, event_row["id"]),
    )
    conn.commit()
    logger.info("ticktick created event_id=%s task_id=%s", event_row["id"], new_id)
    return new_id


# ---------------------------------------------------------------------------
# Sanity / smoke tests
# ---------------------------------------------------------------------------


def smoke_test() -> None:
    """Verify auth + list lookup. Creates the list if it doesn't exist yet."""
    pid = find_or_create_list()
    print(f"TickTick OK: list {LIST_NAME!r} id={pid}")
