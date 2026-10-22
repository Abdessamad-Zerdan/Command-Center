"""Focus-timer sessions and time logged per task.

Part of the queries package — see queries/__init__.py."""

from datetime import datetime
from typing import Any

from command_center.config import TZ
from command_center.db import session


def start_pomodoro_session(
    task_name: str, planned_minutes: int, task_source_id: int | None = None
) -> int:
    """Opens a session row the instant work starts, status='in_progress'
    — the row exists and is the source of truth from second one, rather
    than only being written once at the end. ended_at has no real value
    yet (NOT NULL, so it's seeded to started_at as a placeholder) —
    finish_pomodoro_session overwrites it with the true end time; never
    read ended_at while status='in_progress'.
    """
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        cursor = conn.execute(
            """
            INSERT INTO pomodoro_sessions
                (task_name, task_source_id, planned_minutes, elapsed_seconds,
                 started_at, ended_at, status)
            VALUES (?, ?, ?, 0, ?, ?, 'in_progress')
            """,
            (task_name, task_source_id, planned_minutes, now, now),
        )
        return cursor.lastrowid



def update_pomodoro_heartbeat(
    session_id: int,
    elapsed_seconds: int,
    paused_at: str | None = None,
    resumed_at: str | None = None,
) -> bool:
    """Called every 5s while a session is running (and on pause/resume/
    tab-close) so elapsed_seconds is never more than a few seconds stale
    on disk — this is what makes a session started at 25 minutes and
    abandoned at 18 actually log 18, instead of the 0 a browser-close
    with no periodic save would leave behind. Only touches a row that's
    still 'in_progress', so a heartbeat that arrives late (e.g. a
    straggling request) can never resurrect or corrupt an already-
    finished session.
    """
    with session() as conn:
        cursor = conn.execute(
            """
            UPDATE pomodoro_sessions
            SET elapsed_seconds = ?,
                paused_at = COALESCE(?, paused_at),
                resumed_at = COALESCE(?, resumed_at)
            WHERE id = ? AND status = 'in_progress'
            """,
            (elapsed_seconds, paused_at, resumed_at, session_id),
        )
        return cursor.rowcount > 0



def finish_pomodoro_session(
    session_id: int, elapsed_seconds: int, status: str, break_duration_sec: int | None = None
) -> dict[str, Any] | None:
    """status is 'completed' (ran out the full planned duration) or
    'stopped_early' (any other end — reset, or the break-prompt's Skip).
    Returns the finished row, or None if session_id doesn't exist —
    already-finished sessions are still overwritten (idempotent finish,
    e.g. a retried request), same spirit as the heartbeat's status guard
    but finishing is terminal so there's nothing left to protect against.
    """
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        cursor = conn.execute(
            """
            UPDATE pomodoro_sessions
            SET elapsed_seconds = ?, ended_at = ?, status = ?,
                break_duration_sec = COALESCE(?, break_duration_sec)
            WHERE id = ?
            """,
            (elapsed_seconds, now, status, break_duration_sec, session_id),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM pomodoro_sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row)



def list_pomodoro_history(limit: int = 20) -> list[dict[str, Any]]:
    """Excludes 'in_progress' rows — a session still being heartbeat-
    updated would show a stale, confusingly-frozen elapsed_seconds in a
    static history list; the live timer UI already shows its real-time
    progress. get_time_by_task/get_time_logged_by_source_ids
    deliberately don't apply this filter, so accumulated totals still
    count an in-progress session's time as it happens."""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM pomodoro_sessions WHERE status != 'in_progress' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]



def get_time_logged_by_source_ids(item_ids: list[int]) -> dict[int, int]:
    """{item_id: total elapsed_seconds across every session linked to it}
    — powers the "Time logged: 2h 30m" badge on a task card. Only items
    actually passed in get a key back; an item with zero logged time is
    simply absent, not present with 0 (callers use .get(id, 0))."""
    if not item_ids:
        return {}
    with session() as conn:
        placeholders = ",".join("?" for _ in item_ids)
        rows = conn.execute(
            f"""
            SELECT task_source_id, SUM(elapsed_seconds) AS total_seconds
            FROM pomodoro_sessions
            WHERE task_source_id IN ({placeholders})
            GROUP BY task_source_id
            """,
            item_ids,
        ).fetchall()
        return {row["task_source_id"]: row["total_seconds"] for row in rows}



def get_time_by_task(limit: int = 10) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            """
            SELECT task_name, SUM(elapsed_seconds) AS total_seconds,
                   MAX(task_source_id) AS task_source_id
            FROM pomodoro_sessions
            GROUP BY task_name
            ORDER BY total_seconds DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]



def delete_pomodoro_session(session_id: int) -> dict[str, Any] | None:
    """Returns the deleted row (so the caller can adjust an in-memory
    time-by-task total) or None if it didn't exist."""
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM pomodoro_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM pomodoro_sessions WHERE id = ?", (session_id,))
        return dict(row)



def delete_pomodoro_task(task_name: str) -> int:
    """Deletes every session logged under a task name — the "Time by
    task" row's delete. Returns how many sessions went."""
    with session() as conn:
        cursor = conn.execute("DELETE FROM pomodoro_sessions WHERE task_name = ?", (task_name,))
        return cursor.rowcount



def rename_pomodoro_task(old_name: str, new_name: str) -> int:
    """Bulk rename — every session under the old task name moves to the
    new one. Returns the number of sessions affected."""
    with session() as conn:
        cursor = conn.execute(
            "UPDATE pomodoro_sessions SET task_name = ? WHERE task_name = ?",
            (new_name, old_name),
        )
        return cursor.rowcount
