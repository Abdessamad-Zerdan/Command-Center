"""Saved competitions and hackathons.

Part of the queries package — see queries/__init__.py."""

from datetime import datetime
from typing import Any

from command_center.config import TZ
from command_center.db import session


# Columns a caller may set on a competition — the router validates the
# values; this is just the allow-list for the dynamic UPDATE below.
COMPETITION_FIELDS = (
    "title", "url", "kind", "organizer", "deadline", "event_dates",
    "location", "prize", "notes", "status",
)



def create_competition(fields: dict[str, Any]) -> int:
    now = datetime.now(TZ).isoformat()
    columns = [c for c in COMPETITION_FIELDS if c in fields]
    with session() as conn:
        cursor = conn.execute(
            f"INSERT INTO competitions ({', '.join(columns)}, created_at, updated_at) "
            f"VALUES ({', '.join('?' for _ in columns)}, ?, ?)",
            [fields[c] for c in columns] + [now, now],
        )
        return cursor.lastrowid



def list_competitions() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM competitions ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]



def get_competition(competition_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM competitions WHERE id = ?", (competition_id,)).fetchone()
        return dict(row) if row else None



def update_competition(competition_id: int, fields: dict[str, Any]) -> bool:
    columns = [c for c in COMPETITION_FIELDS if c in fields]
    if not columns:
        return get_competition(competition_id) is not None
    with session() as conn:
        cursor = conn.execute(
            f"UPDATE competitions SET {', '.join(f'{c} = ?' for c in columns)}, updated_at = ? WHERE id = ?",
            [fields[c] for c in columns] + [datetime.now(TZ).isoformat(), competition_id],
        )
        return cursor.rowcount > 0



def set_competition_screenshot(competition_id: int, stored_name: str | None, content_type: str | None) -> bool:
    with session() as conn:
        cursor = conn.execute(
            "UPDATE competitions SET screenshot_name = ?, screenshot_type = ?, updated_at = ? WHERE id = ?",
            (stored_name, content_type, datetime.now(TZ).isoformat(), competition_id),
        )
        return cursor.rowcount > 0



def delete_competition(competition_id: int) -> bool:
    """Linked tasks keep existing — only their link is cleared, same
    explicit-cleanup reasoning as delete_registered_project."""
    with session() as conn:
        conn.execute("UPDATE items SET competition_id = NULL WHERE competition_id = ?", (competition_id,))
        # Its map card stays (the user placed it) but stops claiming to
        # be that competition.
        conn.execute("UPDATE map_nodes SET competition_id = NULL WHERE competition_id = ?", (competition_id,))
        cursor = conn.execute("DELETE FROM competitions WHERE id = ?", (competition_id,))
        return cursor.rowcount > 0



def get_competition_open_task_counts() -> dict[int, int]:
    """{competition_id: pending linked tasks} — absent means zero."""
    with session() as conn:
        rows = conn.execute(
            "SELECT competition_id, COUNT(*) AS n FROM items "
            "WHERE competition_id IS NOT NULL AND status = 'pending' GROUP BY competition_id"
        ).fetchall()
        return {row["competition_id"]: row["n"] for row in rows}
