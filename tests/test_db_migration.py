import sqlite3
from pathlib import Path

import pytest

from command_center import db


@pytest.fixture()
def isolated_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _columns(path: Path, table: str = "items") -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def test_fresh_db_gets_new_columns_via_create_table(isolated_db_path: Path) -> None:
    db.init_db()
    columns = _columns(isolated_db_path)
    assert {"due_date", "scheduled_start", "scheduled_end"} <= columns


def test_existing_db_missing_columns_gains_them_via_alter_table(isolated_db_path: Path) -> None:
    # Simulate an existing database created before this migration shipped —
    # a raw items table without the three new columns.
    conn = sqlite3.connect(isolated_db_path)
    conn.execute(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brief_date TEXT NOT NULL,
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
            created_at TEXT NOT NULL,
            UNIQUE (source, source_id)
        )
        """
    )
    conn.commit()
    conn.close()

    assert "due_date" not in _columns(isolated_db_path)

    db.init_db()  # CREATE TABLE IF NOT EXISTS is a no-op here — migration must add the columns

    columns = _columns(isolated_db_path)
    assert {"due_date", "scheduled_start", "scheduled_end"} <= columns


def test_init_db_is_idempotent_on_second_call(isolated_db_path: Path) -> None:
    db.init_db()
    db.init_db()  # must not raise (e.g. "duplicate column" from a naive unconditional ALTER)
    columns = _columns(isolated_db_path)
    assert {"due_date", "scheduled_start", "scheduled_end"} <= columns


def test_fresh_db_briefs_table_gets_is_fixture_column(isolated_db_path: Path) -> None:
    db.init_db()
    assert "is_fixture" in _columns(isolated_db_path, table="briefs")


def test_existing_briefs_table_missing_is_fixture_gains_it_via_alter_table(
    isolated_db_path: Path,
) -> None:
    # Simulate an existing database created before is_fixture shipped.
    conn = sqlite3.connect(isolated_db_path)
    conn.execute(
        """
        CREATE TABLE briefs (
            brief_date TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            degraded_lanes TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    conn.commit()
    conn.close()

    assert "is_fixture" not in _columns(isolated_db_path, table="briefs")

    db.init_db()

    assert "is_fixture" in _columns(isolated_db_path, table="briefs")
