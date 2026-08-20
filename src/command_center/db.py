"""SQLite access. WAL mode for concurrent reads while the scheduler writes."""

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from command_center.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS briefs (
    brief_date TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    degraded_lanes TEXT NOT NULL DEFAULT '[]',
    is_fixture INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date TEXT NOT NULL REFERENCES briefs(brief_date),
    lane TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    why_it_matters TEXT NOT NULL,
    suggested_next_step TEXT NOT NULL,
    priority INTEGER NOT NULL,
    deep_link TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    snoozed_until TEXT,
    due_date TEXT,
    scheduled_start TEXT,
    scheduled_end TEXT,
    project_id INTEGER REFERENCES registered_projects(id),
    sort_order REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    calendar_event_id TEXT,
    calendar_link TEXT,
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date TEXT NOT NULL,
    title TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    attendees TEXT NOT NULL DEFAULT '[]',
    link TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_brief_date ON items (brief_date);
CREATE INDEX IF NOT EXISTS idx_calendar_events_brief_date ON calendar_events (brief_date);

CREATE TABLE IF NOT EXISTS pomodoro_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name TEXT NOT NULL,
    -- Soft reference, same convention as task_events.task_id (see
    -- queries.log_task_event's docstring): a session logged against an
    -- item stays a valid historical time-tracking fact even after that
    -- items row is later completed, moved, or deleted — no FK
    -- constraint to enforce or break.
    task_source_id INTEGER,
    planned_minutes INTEGER NOT NULL,
    elapsed_seconds INTEGER NOT NULL,
    break_duration_sec INTEGER,
    started_at TEXT NOT NULL,
    paused_at TEXT,
    resumed_at TEXT,
    ended_at TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pomodoro_task ON pomodoro_sessions (task_name);

CREATE TABLE IF NOT EXISTS source_config (
    source_name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    last_pulled_at TEXT
);

CREATE TABLE IF NOT EXISTS content_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    section TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events (task_id);
CREATE INDEX IF NOT EXISTS idx_task_events_type_timestamp ON task_events (event_type, timestamp);

CREATE TABLE IF NOT EXISTS triage_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field TEXT NOT NULL,
    match_value TEXT NOT NULL,
    lane TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reflection_cache (
    period_type TEXT NOT NULL,
    period_start TEXT NOT NULL,
    generated_text TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (period_type, period_start)
);

CREATE TABLE IF NOT EXISTS recurring_commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    day_of_week INTEGER NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_recurring_commitments_day_active
    ON recurring_commitments (day_of_week, active);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registered_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    current_sprint TEXT,
    sprint_goal TEXT,
    blockers TEXT,
    target_date TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finance_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount REAL NOT NULL,
    category TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    entry_date TEXT NOT NULL,
    type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_finance_entries_entry_date ON finance_entries (entry_date);

CREATE TABLE IF NOT EXISTS finance_reflection_cache (
    month_start TEXT NOT NULL PRIMARY KEY,
    generated_text TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS setup_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by_ip TEXT
);

CREATE INDEX IF NOT EXISTS idx_setup_invites_token ON setup_invites (token);

CREATE TABLE IF NOT EXISTS triage_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    brief_date TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    from_lane TEXT NOT NULL,
    to_lane TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_triage_corrections_pattern
    ON triage_corrections (source, from_lane, to_lane);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    char_count INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


_MIGRATED_COLUMNS: dict[str, dict[str, str]] = {
    "items": {
        "due_date": "TEXT",
        "scheduled_start": "TEXT",
        "scheduled_end": "TEXT",
        "project_id": "INTEGER REFERENCES registered_projects(id)",
        # Defaults to 0 for every existing row on migration, so they all
        # tie and fall back to the existing `id ASC` sort — exactly
        # preserving today's order until someone actually drags
        # something, at which point it gets a real, distinct value.
        "sort_order": "REAL NOT NULL DEFAULT 0",
        "calendar_event_id": "TEXT",
        "calendar_link": "TEXT",
    },
    "briefs": {
        "is_fixture": "INTEGER NOT NULL DEFAULT 0",
    },
    "pomodoro_sessions": {
        "task_source_id": "INTEGER",
        "break_duration_sec": "INTEGER",
        "paused_at": "TEXT",
        "resumed_at": "TEXT",
    },
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS can't add columns to an already-existing
    table — this fills that gap for every table in _MIGRATED_COLUMNS.
    Cheap and idempotent: one PRAGMA read per table, then an ALTER only
    for whatever's actually missing (zero ALTERs on any DB created after
    a given column shipped, since SCHEMA creates the columns natively
    for fresh DBs).
    """
    for table, columns in _MIGRATED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, col_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        _migrate_columns(conn)
