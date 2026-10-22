"""SQLite access. WAL mode for concurrent reads while the scheduler writes."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from command_center.config import DB_PATH, TZ

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
    competition_id INTEGER,
    map_node_id INTEGER,
    pursuit_id INTEGER,
    source_time TEXT,
    deadline TEXT,
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
    -- 1 = one of triage_rules.BUILTIN_RULES. The user's own rules are
    -- checked before these, so a personal rule always wins.
    builtin INTEGER NOT NULL DEFAULT 0,
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
    -- Shell command that starts the app (run with cwd=path), e.g.
    -- "python backend/run.py" — used by the Projects page's Run button.
    run_command TEXT,
    -- Where the app opens once running, e.g. "http://127.0.0.1:8010".
    -- Run checks this first so clicking it twice doesn't spawn a second
    -- server — it just opens the tab if something's already listening.
    app_url TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- /map — a freeform "mind map" board: hand-placed cards for projects
-- (pinned by reference, see project_id), next month's targets,
-- hackathons, competitions, and research, each positioned by the user
-- via drag rather than any fixed grid/sort order. x/y are percentages
-- (0-100) of the canvas, not pixels, so a resized viewport doesn't
-- need any position migration.
--
-- Multiple named boards (e.g. "September", "October") so cards for
-- different stretches of time don't have to share one crowded canvas.
CREATE TABLE IF NOT EXISTS map_boards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS map_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id INTEGER NOT NULL REFERENCES map_boards(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    target_date TEXT,
    -- Set only for kind='project': the card mirrors that project's live
    -- name/sprint rather than storing its own copy, so it can't drift
    -- out of sync with the Projects page. NULL for every other kind.
    project_id INTEGER REFERENCES registered_projects(id),
    -- Set on a non-label card to draw a hand-drawn connector from a
    -- kind='label' card (e.g. "September") to this one. NULL for label
    -- cards themselves — a label never links to another label.
    linked_label_id INTEGER REFERENCES map_nodes(id),
    competition_id INTEGER,
    pursuit_id INTEGER,
    x REAL NOT NULL,
    y REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_map_nodes_project_id ON map_nodes (project_id);

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

CREATE TABLE IF NOT EXISTS item_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    stored_name TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_attachments_item_id ON item_attachments (item_id);

-- /research and /targets — research topics and goals, the same shape of
-- "saved thing with a status, a date and tasks" as competitions. A Map
-- card of kind research/target stands for one of these (map_nodes.
-- pursuit_id) and mirrors its title/note/date, so the Map and the page
-- can't drift apart.
CREATE TABLE IF NOT EXISTS pursuits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    notes TEXT,
    status TEXT NOT NULL,
    target_date TEXT,
    url TEXT,
    cover_name TEXT,
    cover_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS competitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT,
    kind TEXT NOT NULL DEFAULT 'hackathon',
    organizer TEXT,
    deadline TEXT,
    event_dates TEXT,
    location TEXT,
    prize TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'saved',
    screenshot_name TEXT,
    screenshot_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
        # Soft link to a saved hackathon/competition — like project_id,
        # cleared explicitly when the competition is deleted.
        "competition_id": "INTEGER",
        # Soft link to a /map card (research, target, hackathon, ...) —
        # what a task "belongs to" when that thing only exists as a card.
        # Cleared explicitly when the card is deleted.
        "map_node_id": "INTEGER",
        # Soft link to a research topic / target (pursuits) — cleared
        # explicitly when it's deleted.
        "pursuit_id": "INTEGER",
        # When the thing happened at its source (an email's arrival, a
        # Google Task's last update) — created_at is only when the app
        # first saw it. Shown as the time label on task cards.
        "source_time": "TEXT",
        # A deadline the item itself states (ISO, with its own offset —
        # see deadlines.py). Past → the card shows "Deadline passed".
        "deadline": "TEXT",
        # The model's one-line reason for this task's High/Medium/Low
        # (prioritize.py) — shown on the card so a label can be trusted.
        "priority_reason": "TEXT",
        # 1 once the user sets the priority themselves (dragging to
        # another priority) — prioritize.py never overrides it after that.
        "priority_locked": "INTEGER NOT NULL DEFAULT 0",
    },
    "triage_rules": {
        "builtin": "INTEGER NOT NULL DEFAULT 0",
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
    "registered_projects": {
        "run_command": "TEXT",
        "app_url": "TEXT",
    },
    "map_nodes": {
        # Nullable here (unlike the NOT NULL in SCHEMA's fresh-install
        # CREATE TABLE) since ALTER TABLE ADD COLUMN can't retroactively
        # backfill a NOT NULL default for rows that already exist —
        # _backfill_map_node_boards below fills every row in anyway, so
        # this only stays NULL between that ALTER and the backfill.
        "board_id": "INTEGER REFERENCES map_boards(id)",
        "linked_label_id": "INTEGER REFERENCES map_nodes(id)",
        # Set when the card stands for a saved competition (Send to Map,
        # or the assistant creating one) — the card then shows that
        # competition's details and tasks. Soft link, cleared when the
        # competition is deleted.
        "competition_id": "INTEGER",
        # Set when the card stands for a research topic / target; see the
        # pursuits table.
        "pursuit_id": "INTEGER",
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


def _backfill_map_node_boards(conn: sqlite3.Connection) -> None:
    """Any map_nodes row predating board_id (or inserted between the
    ALTER above and this running) gets attached to a default board
    instead of silently vanishing behind every page's WHERE board_id = ?
    filter, which no board would ever match."""
    orphaned = conn.execute("SELECT COUNT(*) AS n FROM map_nodes WHERE board_id IS NULL").fetchone()["n"]
    if not orphaned:
        return
    board = conn.execute("SELECT id FROM map_boards ORDER BY id ASC LIMIT 1").fetchone()
    if board is None:
        now = datetime.now(TZ).isoformat()
        cursor = conn.execute("INSERT INTO map_boards (name, created_at) VALUES (?, ?)", ("Board 1", now))
        board_id = cursor.lastrowid
    else:
        board_id = board["id"]
    conn.execute("UPDATE map_nodes SET board_id = ? WHERE board_id IS NULL", (board_id,))


PURSUIT_DEFAULT_STATUS = {"research": "idea", "target": "planned"}


def _adopt_map_pursuit_cards(conn: sqlite3.Connection) -> None:
    """Every research/target card on the Map stands for an entry on the
    Research/Targets page. A card that doesn't yet (made before those
    pages existed, or inserted directly) gets one here, carrying its
    title/note/date — and any task linked to the card moves to the entry,
    so it shows on the page too. Idempotent: only unlinked cards."""
    cards = conn.execute(
        "SELECT * FROM map_nodes WHERE kind IN ('research', 'target') AND pursuit_id IS NULL"
    ).fetchall()
    now = datetime.now(TZ).isoformat()
    for card in cards:
        cursor = conn.execute(
            "INSERT INTO pursuits (kind, title, notes, status, target_date, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (card["kind"], card["title"], card["note"] or None, PURSUIT_DEFAULT_STATUS[card["kind"]],
             card["target_date"], card["created_at"] or now, now),
        )
        pursuit_id = cursor.lastrowid
        conn.execute("UPDATE map_nodes SET pursuit_id = ? WHERE id = ?", (pursuit_id, card["id"]))
        conn.execute(
            "UPDATE items SET pursuit_id = ?, map_node_id = NULL WHERE map_node_id = ?", (pursuit_id, card["id"])
        )


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        _migrate_columns(conn)
        _backfill_map_node_boards(conn)
        # Deferred out of SCHEMA: on a DB where map_nodes already existed
        # before board_id did, a CREATE INDEX ... (board_id) inside the
        # same executescript() as the table's own CREATE TABLE IF NOT
        # EXISTS would run before _migrate_columns above ever adds the
        # column, and fail outright.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_map_nodes_board_id ON map_nodes (board_id)")
        # Same reasoning — due_date is a _MIGRATED_COLUMNS entry on items,
        # so this index can't live inside the main executescript() above
        # either, or it fails on any DB that predates that column.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_status_due_date ON items (status, due_date)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_map_node_id ON items (map_node_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_pursuit_id ON items (pursuit_id)")
        _adopt_map_pursuit_cards(conn)
