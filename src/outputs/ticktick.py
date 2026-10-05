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
  GET  /open/v1/project/{projectId}/data  project + its UNDONE tasks (the only listing)
  POST /open/v1/task/{id}                update task
  DELETE /open/v1/project/{pid}/task/{id}  delete task
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

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


class TickTickNotFound(TickTickError):
    """The task id is not among the list's open tasks (completed or deleted)."""


# TickTick has no extended properties, so the natural key rides as a trailing
# content line. Same key as the Calendar extendedProperty (`gcal._event_key`):
# ticker|event_type|start|end, so a rebuilt DB can re-attach (board #456).
KEY_MARKER = "analyst-days key: "


@dataclass(frozen=True)
class PushResult:
    """See gcal.PushResult; `adopted` here means an existing task matched by the
    content marker or, for tasks created before the marker existed, by exact
    title + due date within a day of start_date."""
    task_id: Optional[str]
    action: str


def event_key(event_row) -> str:
    from src.outputs.gcal import _event_key
    return _event_key(event_row)


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
    parts.append("")
    parts.append(f"{KEY_MARKER}{event_key(event_row)}")
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


def write_task(task: dict, title: str, content: str, due_date: str) -> None:
    """Overwrite title / content / due date on a task we already hold as a full
    object (from `list_open_tasks`). POSTs the whole object back so fields we do
    not own (checklist items, tags, priority) survive."""
    data = dict(task)
    data["title"] = title
    data["content"] = content
    data["dueDate"] = _due_date_iso(due_date)
    task_id = data["id"]
    resp = requests.post(
        f"{API_BASE}/task/{task_id}", headers=_headers(), json=data, timeout=15
    )
    _check(resp, f"update task {task_id}")


def update_task(
    list_id: str,
    task_id: str,
    title: str,
    content: str,
    due_date: str,
    open_tasks: Optional[list[dict]] = None,
) -> None:
    """Refresh title / content / due date on an existing task.

    Reads the task from the list's `/project/{id}/data` payload, NOT the
    documented single-task GET `/task/{projectId}/{taskId}`: that endpoint
    returns 404 for valid ids in practice (earnings_agent/ticktick.py
    `_find_task_in_project`, verified 8/8 on 2026-07-22), which would make every
    update look like a deleted task (Codex review r1 on board #456).

    Raises TickTickNotFound when the id is not among the OPEN tasks -- which
    means deleted OR completed; the two are indistinguishable through this API.
    """
    tasks = open_tasks if open_tasks is not None else list_open_tasks(list_id)
    for t in tasks:
        if t.get("id") == task_id:
            write_task(t, title, content, due_date)
            return
    raise TickTickNotFound(f"task {task_id} not among the list's open tasks "
                           "(completed or deleted)")


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


def list_open_tasks(list_id: str) -> list[dict]:
    """Every UNDONE task in the list (`GET /project/{id}/data`). TickTick offers no
    listing of completed tasks, so a task JP already ticked off is invisible here
    and would be re-created after a DB rebuild -- a known, bounded limit."""
    resp = requests.get(f"{API_BASE}/project/{list_id}/data", headers=_headers(), timeout=20)
    _check(resp, "list project tasks")
    data = resp.json() or {}
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return list(tasks or [])


def _due_day(task: dict) -> Optional[date]:
    raw = task.get("dueDate") or task.get("startDate")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def find_existing_task(open_tasks: Iterable[dict], event_row,
                       claimed_ids: Iterable[str] = ()) -> Optional[dict]:
    """The open task that already represents this event, or None.

    1. Content carries `KEY_MARKER + event_key` -- exact, survives title changes.
    2. Legacy (tasks created before the marker, 2026-07 .. 2026-10): exact title
       (`[Investor Day] CI`, i.e. same ticker + type) AND due day within one day of
       start_date (TickTick stores all-day due dates in the list's zone, so the
       UTC day can be off by one). The title restricts the match to one ticker and
       one event type, so the window cannot adopt a different event.
    `claimed_ids` are task ids other rows already hold: a date-corrected sibling
    row's task must not be shared, or `--retire` on one row deletes the other's.
    """
    claimed = set(claimed_ids)
    key = event_key(event_row)
    marker = f"{KEY_MARKER}{key}"
    tasks = [t for t in open_tasks if t.get("id") and t.get("id") not in claimed]
    for t in tasks:
        if marker in (t.get("content") or ""):
            return t
    title = _task_title(event_row)
    start = date.fromisoformat(event_row["start_date"])
    for t in tasks:
        if (t.get("title") or "").strip() != title:
            continue
        due = _due_day(t)
        if due is not None and abs((due - start).days) <= 1:
            return t
    return None


def upsert_event_task(
    conn: sqlite3.Connection,
    event_row,
    list_id: str,
    open_tasks: Optional[list[dict]] = None,
    claimed_ids: Iterable[str] = (),
) -> PushResult:
    """Create, update or re-attach the TickTick task for one event row.

    Persists the id on events.ticktick_task_id. Never blind-creates: a row
    without a stored id is matched against the list's open tasks first
    (`open_tasks`, fetched once per run by the caller; fetched here if None). A
    stored id that is no longer open is left alone, never re-created (completed
    and deleted look identical through this API). Every HTTP error propagates
    so the caller counts it and retries next run (board #456).
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

    def _persist(task_id: str) -> None:
        conn.execute(
            "UPDATE events SET ticktick_task_id = ? WHERE id = ?",
            (task_id, event_row["id"]),
        )
        conn.commit()

    if open_tasks is None:
        open_tasks = list_open_tasks(list_id)

    existing_id = event_row["ticktick_task_id"]
    if existing_id:
        try:
            update_task(list_id, existing_id, title, content, due_date,
                        open_tasks=open_tasks)
            logger.info("ticktick updated event_id=%s task_id=%s",
                        event_row["id"], existing_id)
            return PushResult(existing_id, "updated")
        except TickTickNotFound as e:
            # Completed or deleted -- the API cannot say which, and re-creating a
            # task JP ticked off is a duplicate. Keep the id; do nothing.
            logger.warning(
                "ticktick task not open for event_id=%s task_id=%s; leaving it: %s",
                event_row["id"], existing_id, e,
            )
            return PushResult(existing_id, "unchanged")

    found = find_existing_task(open_tasks, event_row, claimed_ids)
    if found is not None:
        # Update in place from the LISTED object (no single-task GET): writes the
        # marker onto a legacy task too.
        write_task(found, title, content, due_date)
        _persist(found["id"])
        logger.info("ticktick adopted event_id=%s task_id=%s", event_row["id"], found["id"])
        return PushResult(found["id"], "adopted")

    new_id = create_task(list_id, title, content, due_date)
    _persist(new_id)
    logger.info("ticktick created event_id=%s task_id=%s", event_row["id"], new_id)
    return PushResult(new_id, "created")


# ---------------------------------------------------------------------------
# Sanity / smoke tests
# ---------------------------------------------------------------------------


def smoke_test() -> None:
    """Verify auth + list lookup. Creates the list if it doesn't exist yet."""
    pid = find_or_create_list()
    print(f"TickTick OK: list {LIST_NAME!r} id={pid}")
