"""The events-DB state guard (board #456).

`data/events.db` is gitignored and lives between CI runs only as the expiring
`analyst-days-db` GitHub Actions artifact (retention 90 days, the repo maximum).
Until 2026-10-04 a missing artifact was `if_no_artifact_found: warn`: `init_db`
created an empty file, a 14-day discovery scan became the new state, and the
`Save events database` step (`if: always()`) uploaded that as the newest artifact.
Reminder stamps, output ids and every event older than two weeks were gone, and
the run heartbeated `ok`.

This module is the one predicate behind both layers of the fix:

* `python -m src.state_guard --db data/events.db` -- the workflow step right
  after the restore. Exit 1 (and a `.health/crash.txt` the fallback heartbeat
  quotes) unless the DB is present with at least one `events` row, or a
  bootstrap was explicitly requested for THIS run.
* `require_state(...)` -- called by `cmd_weekly` / `cmd_discover` under CI before
  any phase can touch the DB, so deleting the workflow step does not reopen
  the hole.

Bootstrap polarity is deliberately the opposite of earnings_agent's
`EA_CONSENSUS_BOOTSTRAPPED` repo variable. There, the guard is INERT until the
variable is set, which fit a lane with no artifact yet; here the artifact has
existed since 2026-07-06, so the guard is ARMED by default and the escape hatch
is the explicit, one-run `workflow_dispatch` input `bootstrap_db=true`, mapped
to `ANALYST_DAYS_BOOTSTRAP_DB`. A dispatch input cannot outlive its run and a
scheduled run cannot set it, so a bootstrap can never be left switched on the
way `EA_DB_BOOTSTRAPPED`'s inert window was forgotten for ~85 days.

The predicate is "events has >= 1 row", not "the file exists and is non-empty":
a conferences-only or schema-only DB is a ~100 KB SQLite file, and that is the
exact shape the silent reset produces. It is NOT "earliest row <= HISTORY_FLOOR"
(the #439 page's banner predicate): after any legitimate bootstrap that floor is
violated forever and the guard would block every subsequent run.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BOOTSTRAP_ENV = "ANALYST_DAYS_BOOTSTRAP_DB"
BOOTSTRAP_META_KEY = "bootstrapped_at"
DEFAULT_CRASH_FILE = Path(".health") / "crash.txt"


class MissingStateError(RuntimeError):
    """The events DB is absent (or has no events) and no bootstrap was requested."""


def bootstrap_requested(env: Optional[dict] = None) -> bool:
    """Exactly `true` after trim + lowercase -- mirrors earnings_agent's
    normalisation so `TRUE`/` true ` cannot silently leave the guard armed."""
    env = os.environ if env is None else env
    return (env.get(BOOTSTRAP_ENV, "") or "").strip().lower() == "true"


def is_ci(env: Optional[dict] = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("CI") or env.get("GITHUB_ACTIONS"))


def events_row_count(db_path: Path) -> Optional[int]:
    """Number of `events` rows, or None when the file is missing / unreadable /
    has no `events` table. Opened read-only: the guard must never be the thing
    that creates the file it is checking for."""
    db_path = Path(db_path)
    if not db_path.is_file():
        return None
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def state_present(db_path: Path) -> bool:
    return bool(events_row_count(db_path))


def describe_missing(db_path: Path) -> str:
    n = events_row_count(db_path)
    if n is None:
        shape = "is absent (or not a readable SQLite DB with an events table)"
    else:
        shape = f"exists but has {n} events rows"
    return (
        f"STATE GUARD: {db_path} {shape} and {BOOTSTRAP_ENV} is not 'true'.\n"
        "The analyst-days-db artifact was not restored. Realistic cause: every artifact "
        "from a successful master run has expired (90-day retention; 13 weeks with no "
        "successful Monday run) or was deleted. Refusing to start a fresh database, "
        "because the next step would upload the 14-day scan as the new state and "
        "silently lose history, reminder stamps and Calendar/TickTick ids (board #456).\n"
        "If a fresh start IS intended: run monday.yml via the Actions button with "
        "bootstrap_db=true, once."
    )


def require_state(db_path: Path, *, env: Optional[dict] = None) -> bool:
    """Gate a CI run on the state artifact.

    Returns True when the run is a permitted BOOTSTRAP (state absent, bootstrap
    requested) so the caller can mark the run as such, False when state is
    present. Raises MissingStateError when state is absent and no bootstrap was
    requested. Outside CI a fresh dev DB is always allowed (returns False: a dev
    sandbox is not a bootstrap of production).
    """
    env = os.environ if env is None else env
    if state_present(db_path):
        if bootstrap_requested(env):
            print(f"[state-guard] {BOOTSTRAP_ENV}=true but state is present at {db_path}; "
                  "using the restored database, bootstrap not needed.")
        return False
    if not is_ci(env):
        return False
    if bootstrap_requested(env):
        print(f"[state-guard] BOOTSTRAP: no state at {db_path}; starting a fresh "
              "database because this run explicitly asked for it.")
        return True
    raise MissingStateError(describe_missing(db_path))


def record_bootstrap(conn: sqlite3.Connection, when: Optional[str] = None) -> str:
    """Persist `schema_meta.bootstrapped_at` so consumers (the #439 listing page's
    "history is missing" banner) can tell a deliberate restart from a silent loss.
    Only the first bootstrap is kept: a later one must not erase the earlier floor."""
    when = when or datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES(?, ?)",
        (BOOTSTRAP_META_KEY, when),
    )
    conn.commit()
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (BOOTSTRAP_META_KEY,)
    ).fetchone()
    return row[0]


def bootstrapped_at(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (BOOTSTRAP_META_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fail unless the events DB state is present.")
    ap.add_argument("--db", default="data/events.db")
    ap.add_argument("--crash-file", default=str(DEFAULT_CRASH_FILE),
                    help="Where to write the diagnosis for the fallback heartbeat.")
    args = ap.parse_args(argv)
    db_path = Path(args.db)
    try:
        boot = require_state(db_path, env=dict(os.environ, CI="1"))
    except MissingStateError as e:
        crash = Path(args.crash_file)
        try:
            crash.parent.mkdir(parents=True, exist_ok=True)
            crash.write_text(str(e), encoding="utf-8")
        except OSError as werr:  # the diagnosis must still reach the log
            print(f"[state-guard] could not write {crash}: {werr!r}", file=sys.stderr)
        print(f"::error::{str(e).splitlines()[0]}", file=sys.stderr)
        print(str(e), file=sys.stderr)
        return 1
    n = events_row_count(db_path)
    print(f"[state-guard] ok: {db_path} events rows={n} bootstrap={boot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
