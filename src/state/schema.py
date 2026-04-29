"""
SQLite schema + non-destructive migrations for analyst-days.

Schema is versioned via the schema_meta.schema_version row. Migrations only add
columns / tables; they never drop or rewrite existing data. Bump
CURRENT_SCHEMA_VERSION when you add a migration step.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CURRENT_SCHEMA_VERSION = 1


EVENT_TYPES = (
    "investor_day",
    "analyst_day",
    "rd_day",
    "capital_markets_day",
    "conference",
)

EVENT_STATUSES = (
    "discovered",
    "tentative",
    "confirmed",
    "reminded_30",
    "reminded_7",
    "day_of",
    "completed",
    "historical",
)

SOURCE_TYPES = (
    "8K",
    "IR_PAGE",
    "PRESS_RELEASE",
    "TAVILY_HIT",
    "MANUAL",
)


def _create_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            company_name    TEXT,
            event_type      TEXT NOT NULL,
            start_date      TEXT,
            end_date        TEXT,
            multi_day       INTEGER NOT NULL DEFAULT 0,
            date_imprecise  INTEGER NOT NULL DEFAULT 0,
            imprecise_hint  TEXT,
            status          TEXT NOT NULL DEFAULT 'discovered',
            confidence      REAL,
            first_seen_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            confirmed_at    TEXT,
            slack_posted_at TEXT,
            calendar_event_id TEXT,
            ticktick_task_id  TEXT,
            reminded_30_at  TEXT,
            reminded_7_at   TEXT,
            day_of_at       TEXT,
            notes           TEXT,
            UNIQUE(ticker, event_type, start_date)
        );

        CREATE INDEX IF NOT EXISTS idx_events_status_date
            ON events(status, start_date);
        CREATE INDEX IF NOT EXISTS idx_events_ticker
            ON events(ticker);

        CREATE TABLE IF NOT EXISTS event_sources (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        INTEGER NOT NULL,
            source_type     TEXT NOT NULL,
            source_url      TEXT,
            source_excerpt  TEXT,
            accession_no    TEXT,
            retrieved_at    TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_event_sources_event
            ON event_sources(event_id);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_event_sources_url
            ON event_sources(event_id, source_url);

        CREATE TABLE IF NOT EXISTS conferences (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            short_name  TEXT,
            start_date  TEXT NOT NULL,
            end_date    TEXT NOT NULL,
            location    TEXT,
            url         TEXT,
            notes       TEXT
        );

        CREATE TABLE IF NOT EXISTS conference_presentations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conference_id   INTEGER NOT NULL,
            ticker          TEXT NOT NULL,
            company_name    TEXT,
            slot_datetime   TEXT,
            source_url      TEXT,
            FOREIGN KEY(conference_id) REFERENCES conferences(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_conf_pres_conference
            ON conference_presentations(conference_id);
        CREATE INDEX IF NOT EXISTS idx_conf_pres_ticker
            ON conference_presentations(ticker);

        CREATE TABLE IF NOT EXISTS run_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_type    TEXT NOT NULL,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            status      TEXT,
            summary     TEXT
        );
        """
    )


# Ordered list of migrations. Each entry is (target_version, callable).
# Call only the migrations whose target_version > current.
_MIGRATIONS: list[tuple[int, callable]] = [
    (1, _create_v1),
]


def _read_version(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _write_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create the DB if missing and run any pending migrations.

    Returns an open connection with foreign keys enabled.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    # Bootstrap schema_meta if the DB is brand new — needed before _read_version.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )

    current = _read_version(conn)
    for target, migration in _MIGRATIONS:
        if target > current:
            migration(conn)
            _write_version(conn, target)
    conn.commit()
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return _read_version(conn)
