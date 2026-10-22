"""What actually happened in a period, for the reflection to talk about:
which tasks were finished (with the Pomodoro time each got), where the
focus time went, and what stayed open the whole period.

The reflection used to see counts only ("12 created, 9 completed"), so it
could only ever say the numbers back. Task titles do go to the model now
(the owner chose that; README's data table says so) — but only titles,
lanes and minutes, never a task's notes, mail bodies or attachments.

Same half-open [start, end) convention and plain ISO string comparison as
aggregations.py.
"""

from datetime import date
from typing import Any

from command_center.db import session

_MAX_DONE = 25
_MAX_FOCUS_TASKS = 8
_MAX_OPEN = 8


def _minutes(seconds: int | None) -> int:
    return round((seconds or 0) / 60)


def completed_tasks(start: date, end: date) -> list[dict[str, Any]]:
    """Tasks completed in the period, newest first: title (current, else
    as created), lane as created, and the Pomodoro minutes logged on it."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT e.task_id,
                   COALESCE(i.title, json_extract(c.metadata_json, '$.title'), 'Untitled') AS title,
                   COALESCE(json_extract(c.metadata_json, '$.lane'), i.lane, 'unknown') AS lane,
                   (SELECT SUM(p.elapsed_seconds) FROM pomodoro_sessions p WHERE p.task_source_id = e.task_id) AS seconds,
                   MAX(e.timestamp) AS done_at
            FROM task_events e
            LEFT JOIN task_events c ON c.task_id = e.task_id AND c.event_type = 'created'
            LEFT JOIN items i ON i.id = e.task_id
            WHERE e.event_type = 'completed' AND e.timestamp >= ? AND e.timestamp < ?
            GROUP BY e.task_id
            ORDER BY done_at DESC
            LIMIT ?
            """,
            (start.isoformat(), end.isoformat(), _MAX_DONE),
        ).fetchall()
    return [{"title": r["title"], "lane": r["lane"], "focus_minutes": _minutes(r["seconds"])} for r in rows]


def focus_time(start: date, end: date) -> dict[str, Any]:
    """Pomodoro sessions started in the period: totals, how many ran to
    the end vs were stopped early, and the tasks that got the most time."""
    with session() as conn:
        total = conn.execute(
            """
            SELECT COUNT(*) AS sessions, COALESCE(SUM(elapsed_seconds), 0) AS seconds,
                   SUM(status = 'completed') AS finished, SUM(status = 'stopped_early') AS stopped
            FROM pomodoro_sessions WHERE started_at >= ? AND started_at < ?
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        by_task = conn.execute(
            """
            SELECT task_name, COUNT(*) AS sessions, SUM(elapsed_seconds) AS seconds
            FROM pomodoro_sessions WHERE started_at >= ? AND started_at < ?
            GROUP BY task_name ORDER BY seconds DESC LIMIT ?
            """,
            (start.isoformat(), end.isoformat(), _MAX_FOCUS_TASKS),
        ).fetchall()
    return {
        "total_minutes": _minutes(total["seconds"]),
        "sessions": total["sessions"] or 0,
        "finished_sessions": total["finished"] or 0,
        "stopped_early": total["stopped"] or 0,
        "by_task": [
            {"task": r["task_name"], "minutes": _minutes(r["seconds"]), "sessions": r["sessions"]}
            for r in by_task
        ],
    }


def open_all_period(start: date, end: date) -> list[dict[str, Any]]:
    """Tasks created before the period that were still not completed at
    its end — what kept slipping. Same "latest event before end" rule as
    aggregations.rollover_count, oldest first."""
    with session() as conn:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT te.task_id, te.event_type,
                       ROW_NUMBER() OVER (PARTITION BY te.task_id ORDER BY te.timestamp DESC, te.id DESC) AS rn
                FROM task_events te
                WHERE te.timestamp < ?
                  AND te.task_id IN (SELECT task_id FROM task_events WHERE event_type = 'created' AND timestamp < ?)
            )
            SELECT r.task_id,
                   COALESCE(i.title, json_extract(c.metadata_json, '$.title'), 'Untitled') AS title,
                   COALESCE(json_extract(c.metadata_json, '$.lane'), i.lane, 'unknown') AS lane,
                   substr(c.timestamp, 1, 10) AS since
            FROM ranked r
            LEFT JOIN task_events c ON c.task_id = r.task_id AND c.event_type = 'created'
            LEFT JOIN items i ON i.id = r.task_id
            WHERE r.rn = 1 AND r.event_type NOT IN ('completed', 'dismissed')
            ORDER BY since ASC
            LIMIT ?
            """,
            (end.isoformat(), start.isoformat(), _MAX_OPEN),
        ).fetchall()
    return [{"title": r["title"], "lane": r["lane"], "open_since": r["since"]} for r in rows]


def collect(start: date, end: date) -> dict[str, Any]:
    return {
        "done": completed_tasks(start, end),
        "focus": focus_time(start, end),
        "still_open": open_all_period(start, end),
    }
