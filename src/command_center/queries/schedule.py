"""Recurring commitments and scheduling tasks into free time.

Part of the queries package — see queries/__init__.py."""

from typing import Any

from command_center.db import session
from command_center.queries.items import log_task_event


def list_recurring_commitments(
    day_of_week: int | None = None, active_only: bool = False
) -> list[dict[str, Any]]:
    """day_of_week=None returns all rows (the /settings/schedule list
    view); day_of_week=0..6 scopes to one day (availability.py).
    active_only=True adds AND active = 1 (availability.py always passes
    True; the settings page passes False so inactive rows still show,
    editable, in the list)."""
    query = "SELECT * FROM recurring_commitments WHERE 1=1"
    params: list[Any] = []
    if day_of_week is not None:
        query += " AND day_of_week = ?"
        params.append(day_of_week)
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY day_of_week ASC, start_time ASC"
    with session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]



def create_recurring_commitment(title: str, day_of_week: int, start_time: str, end_time: str) -> int:
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO recurring_commitments (title, day_of_week, start_time, end_time, active) "
            "VALUES (?, ?, ?, ?, 1)",
            (title, day_of_week, start_time, end_time),
        )
        return cursor.lastrowid



def update_recurring_commitment(
    commitment_id: int,
    title: str | None = None,
    day_of_week: int | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    active: bool | None = None,
) -> bool:
    """Partial update, same pattern as update_source_config. Returns True
    if a row was matched."""
    sets: list[str] = []
    params: list[Any] = []
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if day_of_week is not None:
        sets.append("day_of_week = ?")
        params.append(day_of_week)
    if start_time is not None:
        sets.append("start_time = ?")
        params.append(start_time)
    if end_time is not None:
        sets.append("end_time = ?")
        params.append(end_time)
    if active is not None:
        sets.append("active = ?")
        params.append(1 if active else 0)
    if not sets:
        return False
    params.append(commitment_id)
    with session() as conn:
        cursor = conn.execute(
            f"UPDATE recurring_commitments SET {', '.join(sets)} WHERE id = ?", params
        )
        return cursor.rowcount > 0



def delete_recurring_commitment(commitment_id: int) -> bool:
    with session() as conn:
        cursor = conn.execute("DELETE FROM recurring_commitments WHERE id = ?", (commitment_id,))
        return cursor.rowcount > 0



def list_unscheduled_task_candidates(week_end: str) -> list[dict[str, Any]]:
    """Task-like (source is google_tasks or manual — the only two
    sources a scheduling suggestion makes sense for), pending, no
    scheduled_start yet, due inside-or-before week_end or no due date
    at all (still eligible, sorted last by the caller)."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT id, lane, title, priority, due_date
            FROM items
            WHERE status = 'pending'
              AND source IN ('google_tasks', 'manual')
              AND scheduled_start IS NULL
              AND (due_date IS NULL OR due_date <= ?)
            ORDER BY id ASC
            """,
            (week_end,),
        ).fetchall()
        return [dict(row) for row in rows]



def schedule_item(item_id: int, scheduled_start: str, scheduled_end: str) -> bool:
    """Writes an accepted placement (ISO datetime strings). Only pending
    items can be scheduled. Returns True if a row was updated."""
    with session() as conn:
        row = conn.execute(
            "SELECT lane, source, title FROM items WHERE id = ? AND status = 'pending'",
            (item_id,),
        ).fetchone()
        cursor = conn.execute(
            "UPDATE items SET scheduled_start = ?, scheduled_end = ? WHERE id = ? AND status = 'pending'",
            (scheduled_start, scheduled_end, item_id),
        )
        updated = cursor.rowcount > 0
        if updated and row is not None:
            log_task_event(
                item_id,
                "scheduled",
                metadata={
                    "lane": row["lane"],
                    "source": row["source"],
                    "title": row["title"],
                    "scheduled_start": scheduled_start,
                    "scheduled_end": scheduled_end,
                },
                conn=conn,
            )
        return updated
