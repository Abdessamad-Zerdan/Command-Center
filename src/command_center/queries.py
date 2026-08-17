"""Read/write helpers used by the routes. Thin wrappers around sqlite3.Row."""

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from command_center.config import LANES, TZ
from command_center.db import session

# No caller passes "pending" to set_item_status today (no reopen/un-snooze
# endpoint exists yet), but mapping it now means a future reopen feature
# needs zero changes here.
_STATUS_EVENT_MAP = {"done": "completed", "snoozed": "snoozed", "pending": "reopened"}


def log_task_event(
    task_id: int,
    event_type: str,
    metadata: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Append-only — never updated or deleted, same pattern as
    log_tool_call. `task_id` is a soft reference (see db.py's SCHEMA) —
    it stays a valid historical fact even after the items row it pointed
    at is later mutated, replaced, or deleted.

    Pass `conn` when called from inside another function's own
    `with session() as conn:` block, so the event insert commits in the
    same transaction as the status change it's recording — every hook in
    this module does that, so the event log can never fall out of sync
    with the mutation that produced it.
    """
    payload = (task_id, event_type, datetime.now(TZ).isoformat(), json.dumps(metadata or {}))
    sql = (
        "INSERT INTO task_events (task_id, event_type, timestamp, metadata_json) "
        "VALUES (?, ?, ?, ?)"
    )
    if conn is not None:
        conn.execute(sql, payload)
        return
    with session() as c:
        c.execute(sql, payload)


def get_brief(brief_date: str) -> dict[str, Any] | None:
    with session() as conn:
        brief = conn.execute(
            "SELECT * FROM briefs WHERE brief_date = ?", (brief_date,)
        ).fetchone()
        if brief is None:
            return None

        rows = conn.execute(
            "SELECT * FROM items WHERE brief_date = ? AND status = 'pending' "
            "ORDER BY priority ASC, id ASC",
            (brief_date,),
        ).fetchall()

        lanes: dict[str, list[dict[str, Any]]] = {lane: [] for lane in LANES}
        for row in rows:
            lanes[row["lane"]].append(dict(row))

        events = conn.execute(
            "SELECT * FROM calendar_events WHERE brief_date = ? ORDER BY start_time ASC",
            (brief_date,),
        ).fetchall()
        events_out = []
        for ev in events:
            ev_dict = dict(ev)
            ev_dict["attendees"] = json.loads(ev_dict["attendees"])
            events_out.append(ev_dict)

        return {
            "brief_date": brief["brief_date"],
            "generated_at": brief["generated_at"],
            "degraded_lanes": json.loads(brief["degraded_lanes"]),
            "is_fixture": bool(brief["is_fixture"]),
            "lanes": lanes,
            "events": events_out,
        }


def list_history_dates() -> list[str]:
    with session() as conn:
        rows = conn.execute(
            "SELECT brief_date FROM briefs ORDER BY brief_date DESC"
        ).fetchall()
        return [row["brief_date"] for row in rows]


def set_item_status(item_id: int, status: str, snoozed_until: str | None = None) -> None:
    with session() as conn:
        row = conn.execute(
            "SELECT lane, source, title FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        conn.execute(
            "UPDATE items SET status = ?, snoozed_until = ? WHERE id = ?",
            (status, snoozed_until, item_id),
        )
        event_type = _STATUS_EVENT_MAP.get(status)
        if event_type and row is not None:
            log_task_event(
                item_id,
                event_type,
                metadata={"lane": row["lane"], "source": row["source"], "title": row["title"]},
                conn=conn,
            )


def move_item_to_date(item_id: int, new_brief_date: str, lane: str | None = None) -> bool:
    """Moves an item to a different day's brief — resets status to
    'pending' (so it actually shows up there) and optionally changes
    lane in the same update. Ensures the target day's briefs row exists
    first, same idiom as create_manual_item. Returns True if a row
    was matched.
    """
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        conn.execute(
            "INSERT INTO briefs (brief_date, generated_at, degraded_lanes) "
            "VALUES (?, ?, '[]') ON CONFLICT(brief_date) DO NOTHING",
            (new_brief_date, now),
        )
        row = conn.execute(
            "SELECT lane, source, title FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return False

        sets = ["brief_date = ?", "status = 'pending'"]
        params: list[Any] = [new_brief_date]
        if lane is not None:
            sets.append("lane = ?")
            params.append(lane)
        params.append(item_id)
        cursor = conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", params)
        moved = cursor.rowcount > 0
        if moved:
            log_task_event(
                item_id,
                "moved",
                metadata={
                    "lane": lane or row["lane"],
                    "source": row["source"],
                    "title": row["title"],
                    "to_date": new_brief_date,
                },
                conn=conn,
            )
        return moved


def list_recent_pending_items(before_date: str, days: int = 7) -> list[dict[str, Any]]:
    """Still-pending items from the `days` days immediately before
    `before_date` (not including it) — lets the assistant answer "what
    was on yesterday's brief" and look up ids to hand to
    move_item_to_date, without needing a brand new per-date lookup tool
    for every day someone might ask about."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT * FROM items
            WHERE status = 'pending'
              AND brief_date < ?
              AND brief_date >= date(?, ?)
            ORDER BY brief_date DESC, priority ASC, id ASC
            """,
            (before_date, before_date, f"-{days} days"),
        ).fetchall()
        return [dict(row) for row in rows]


def touch_brief_generated_at(brief_date: str) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE briefs SET generated_at = ? WHERE brief_date = ?",
            (datetime.now(TZ).isoformat(), brief_date),
        )


def save_triage_results(
    brief_date: str,
    triaged_items: list[dict[str, Any]],
    calendar_events: list[dict[str, Any]],
    degraded_sources: list[str],
    sources_attempted: list[str],
    force: bool = False,
) -> None:
    """Writes a pipeline run — either the full pipeline or a single source.

    `sources_attempted` scopes the two operations that used to assume
    "every source, every time": `calendar_events` is only replaced if
    "calendar" is in it (otherwise a non-calendar single-source pull would
    wipe today's timeline strip), and `degraded_lanes` is merged rather
    than overwritten — sources just retried drop out of the stored list,
    then whatever failed *this* round is added back in. The full-pipeline
    caller passes all source names, so its behavior is unchanged.

    `force=True` re-inserts items even if their (source, source_id) was
    already triaged on a previous day — the explicit dedup override from
    the original spec.
    """
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        existing = conn.execute(
            "SELECT degraded_lanes FROM briefs WHERE brief_date = ?", (brief_date,)
        ).fetchone()
        prior_degraded = json.loads(existing["degraded_lanes"]) if existing else []
        merged_degraded = [s for s in prior_degraded if s not in sources_attempted]
        merged_degraded += [s for s in degraded_sources if s not in merged_degraded]

        conn.execute(
            """
            INSERT INTO briefs (brief_date, generated_at, degraded_lanes, is_fixture)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(brief_date) DO UPDATE SET
                generated_at = excluded.generated_at,
                degraded_lanes = excluded.degraded_lanes,
                is_fixture = 0
            """,
            (brief_date, now, json.dumps(merged_degraded)),
        )

        insert_verb = "INSERT OR REPLACE" if force else "INSERT OR IGNORE"
        for item in triaged_items:
            existing = conn.execute(
                "SELECT id FROM items WHERE source = ? AND source_id = ?",
                (item["source"], item["source_id"]),
            ).fetchone()
            cursor = conn.execute(
                f"""
                {insert_verb} INTO items
                    (brief_date, lane, source, source_id, title, why_it_matters,
                     suggested_next_step, priority, deep_link, due_date, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    brief_date,
                    item["lane"],
                    item["source"],
                    item["source_id"],
                    item["title"],
                    item["why_it_matters"],
                    item["suggested_next_step"],
                    item["priority"],
                    item["deep_link"],
                    item.get("due_date"),
                    now,
                ),
            )
            # Only a genuinely first-ever sighting of (source, source_id)
            # counts as "created" — a force=True re-pull of an already-known
            # pair uses INSERT OR REPLACE, which deletes+reinserts under a
            # new id and would otherwise fabricate a false "created" event
            # for something that isn't new user activity.
            if existing is None:
                log_task_event(
                    cursor.lastrowid,
                    "created",
                    metadata={"lane": item["lane"], "source": item["source"], "title": item["title"]},
                    conn=conn,
                )

        if "calendar" in sources_attempted:
            conn.execute("DELETE FROM calendar_events WHERE brief_date = ?", (brief_date,))
            for ev in calendar_events:
                conn.execute(
                    """
                    INSERT INTO calendar_events
                        (brief_date, title, start_time, end_time, attendees, link)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        brief_date,
                        ev["title"],
                        ev["start_time"],
                        ev["end_time"],
                        ev["attendees"],
                        ev["link"],
                    ),
                )


def list_source_configs() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM source_config ORDER BY source_name ASC"
        ).fetchall()
        return [dict(row) for row in rows]


def seed_source_config(
    source_names: list[str],
    default_interval_minutes: int = 60,
    overrides: dict[str, int] | None = None,
) -> None:
    """Seeds a row per source, `last_pulled_at` set to now rather than
    NULL — that's what keeps the scheduler from firing an immediate pull
    on first boot, since it only fires once `interval_minutes` have
    elapsed since `last_pulled_at`. Existing rows are left untouched.

    `overrides` lets specific sources start at a different interval than
    `default_interval_minutes` (e.g. content-discovery sources that
    don't need same-day freshness) — only used the very first time a
    source's row is created, never retroactively.
    """
    now = datetime.now(TZ).isoformat()
    overrides = overrides or {}
    with session() as conn:
        for name in source_names:
            interval = overrides.get(name, default_interval_minutes)
            conn.execute(
                """
                INSERT OR IGNORE INTO source_config
                    (source_name, enabled, interval_minutes, last_pulled_at)
                VALUES (?, 1, ?, ?)
                """,
                (name, interval, now),
            )


def update_source_config(
    source_name: str, enabled: bool | None = None, interval_minutes: int | None = None
) -> None:
    with session() as conn:
        if enabled is not None:
            conn.execute(
                "UPDATE source_config SET enabled = ? WHERE source_name = ?",
                (1 if enabled else 0, source_name),
            )
        if interval_minutes is not None:
            conn.execute(
                "UPDATE source_config SET interval_minutes = ? WHERE source_name = ?",
                (interval_minutes, source_name),
            )


def touch_source_last_pulled(source_name: str) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE source_config SET last_pulled_at = ? WHERE source_name = ?",
            (datetime.now(TZ).isoformat(), source_name),
        )


def create_pomodoro_session(
    task_name: str,
    planned_minutes: int,
    elapsed_seconds: int,
    started_at: str,
    ended_at: str,
    status: str,
) -> int:
    with session() as conn:
        cursor = conn.execute(
            """
            INSERT INTO pomodoro_sessions
                (task_name, planned_minutes, elapsed_seconds, started_at, ended_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_name, planned_minutes, elapsed_seconds, started_at, ended_at, status),
        )
        return cursor.lastrowid


def list_pomodoro_history(limit: int = 20) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM pomodoro_sessions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def create_manual_item(
    brief_date: str, lane: str, title: str, due_date: str | None = None, project_id: int | None = None
) -> int:
    """A user-typed task, not from any triage source. Distinct source_id
    per call (uuid4) since real sources dedup on (source, source_id) but a
    manual task has no natural stable id to dedup against."""
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        conn.execute(
            "INSERT INTO briefs (brief_date, generated_at, degraded_lanes) "
            "VALUES (?, ?, '[]') ON CONFLICT(brief_date) DO NOTHING",
            (brief_date, now),
        )
        cursor = conn.execute(
            """
            INSERT INTO items
                (brief_date, lane, source, source_id, title, why_it_matters,
                 suggested_next_step, priority, deep_link, due_date, project_id, status, created_at)
            VALUES (?, ?, 'manual', ?, ?, '', '', 2, '', ?, ?, 'pending', ?)
            """,
            (brief_date, lane, str(uuid.uuid4()), title, due_date, project_id, now),
        )
        item_id = cursor.lastrowid
        log_task_event(
            item_id, "created",
            metadata={"lane": lane, "source": "manual", "title": title},
            conn=conn,
        )
        return item_id


def create_synced_task_item(
    brief_date: str,
    lane: str,
    title: str,
    source_id: str,
    due_date: str | None = None,
    project_id: int | None = None,
) -> int:
    """Inserts a google_tasks-sourced item directly, with no triage pass
    — used right after the assistant's create_task tool creates the real
    Google Task, so it shows up in the brief immediately instead of
    waiting on a full pipeline re-pull-and-retriage (an LLM call, easily
    several seconds). why_it_matters/suggested_next_step stay blank,
    same as create_manual_item's manual items — there's no triage output
    to fill them with, and item_card.html already renders fine without
    them. `source_id` is the real Google Tasks id (not a generated
    uuid4, unlike create_manual_item), so a later scheduled pull's own
    INSERT OR IGNORE correctly recognizes this row as already-known
    rather than duplicating it.
    """
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        conn.execute(
            "INSERT INTO briefs (brief_date, generated_at, degraded_lanes) "
            "VALUES (?, ?, '[]') ON CONFLICT(brief_date) DO NOTHING",
            (brief_date, now),
        )
        cursor = conn.execute(
            """
            INSERT INTO items
                (brief_date, lane, source, source_id, title, why_it_matters,
                 suggested_next_step, priority, deep_link, due_date, project_id, status, created_at)
            VALUES (?, ?, 'google_tasks', ?, ?, '', '', 2, '', ?, ?, 'pending', ?)
            """,
            (brief_date, lane, source_id, title, due_date, project_id, now),
        )
        item_id = cursor.lastrowid
        log_task_event(
            item_id, "created",
            metadata={"lane": lane, "source": "google_tasks", "title": title},
            conn=conn,
        )
        return item_id


def get_time_by_task(limit: int = 10) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            """
            SELECT task_name, SUM(elapsed_seconds) AS total_seconds
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


def rename_pomodoro_task(old_name: str, new_name: str) -> int:
    """Bulk rename — every session under the old task name moves to the
    new one. Returns the number of sessions affected."""
    with session() as conn:
        cursor = conn.execute(
            "UPDATE pomodoro_sessions SET task_name = ? WHERE task_name = ?",
            (new_name, old_name),
        )
        return cursor.rowcount


def update_item_title(item_id: int, title: str) -> bool:
    """Only manual items are editable — enforced here, not just in the UI.
    Returns True if a row was actually updated."""
    with session() as conn:
        row = conn.execute(
            "SELECT lane, source FROM items WHERE id = ? AND source = 'manual'", (item_id,)
        ).fetchone()
        cursor = conn.execute(
            "UPDATE items SET title = ? WHERE id = ? AND source = 'manual'",
            (title, item_id),
        )
        updated = cursor.rowcount > 0
        if updated and row is not None:
            log_task_event(
                item_id, "updated",
                metadata={"lane": row["lane"], "source": row["source"], "title": title},
                conn=conn,
            )
        return updated


def update_item_lane(item_id: int, lane: str) -> bool:
    """Drag-and-drop recategorization — any item can move lanes, not just
    manual ones (triage can be wrong). Returns True if a row was updated."""
    with session() as conn:
        row = conn.execute("SELECT source, title FROM items WHERE id = ?", (item_id,)).fetchone()
        cursor = conn.execute(
            "UPDATE items SET lane = ? WHERE id = ?",
            (lane, item_id),
        )
        updated = cursor.rowcount > 0
        if updated and row is not None:
            # Snapshot the NEW lane — metadata is always "state at event
            # time," and this event's whole point is recording the move.
            log_task_event(
                item_id, "updated",
                metadata={"lane": lane, "source": row["source"], "title": row["title"]},
                conn=conn,
            )
        return updated


def update_item_project(item_id: int, project_id: int | None) -> bool:
    """Reassign (or clear, if project_id is None) an item's linked
    project. Any item can be assigned, not just manual ones — same
    "any item can move" policy as update_item_lane, since this is
    organizational metadata, not content. Returns True if a row was
    updated."""
    with session() as conn:
        row = conn.execute("SELECT lane, source, title FROM items WHERE id = ?", (item_id,)).fetchone()
        cursor = conn.execute(
            "UPDATE items SET project_id = ? WHERE id = ?",
            (project_id, item_id),
        )
        updated = cursor.rowcount > 0
        if updated and row is not None:
            log_task_event(
                item_id, "updated",
                metadata={"lane": row["lane"], "source": row["source"], "title": row["title"], "project_id": project_id},
                conn=conn,
            )
        return updated


def list_items_by_project(project_id: int) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE project_id = ? ORDER BY priority ASC, id ASC",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def update_item_from_task(source_id: str, title: str | None = None, status: str | None = None) -> bool:
    """Keeps the local mirror of a Google Tasks item in sync right after
    the assistant updates/completes it via tool-calling. A scheduled
    re-pull wouldn't touch this row on its own — save_triage_results
    uses INSERT OR IGNORE, so an already-existing (source, source_id)
    row is left untouched, not refreshed. Returns True if a row matched.
    """
    if title is None and status is None:
        return False
    sets = []
    params: list[Any] = []
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    params.append(source_id)
    with session() as conn:
        row = conn.execute(
            "SELECT id, lane, source, title FROM items WHERE source = 'google_tasks' AND source_id = ?",
            (source_id,),
        ).fetchone()
        cursor = conn.execute(
            f"UPDATE items SET {', '.join(sets)} WHERE source = 'google_tasks' AND source_id = ?",
            params,
        )
        updated = cursor.rowcount > 0
        if updated and row is not None:
            event_type = _STATUS_EVENT_MAP.get(status) if status is not None else "updated"
            log_task_event(
                row["id"],
                event_type,
                metadata={
                    "lane": row["lane"],
                    "source": row["source"],
                    "title": title if title is not None else row["title"],
                },
                conn=conn,
            )
        return updated


def log_tool_call(tool: str, args: dict[str, Any], status: str) -> None:
    """Append-only — each event (proposed, then confirmed/cancelled/failed)
    is its own row, so the full attempt history stays visible for
    debugging rather than being overwritten in place."""
    with session() as conn:
        conn.execute(
            "INSERT INTO tool_call_log (tool, args_json, status, created_at) "
            "VALUES (?, ?, ?, ?)",
            (tool, json.dumps(args), status, datetime.now(TZ).isoformat()),
        )


_DEFAULT_APP_SETTINGS = {"day_bounds_start": "07:00", "day_bounds_end": "22:00"}


def seed_app_settings() -> None:
    """Seeds default day-bounds settings on first run. INSERT OR IGNORE,
    same idiom as seed_source_config — existing rows are left untouched."""
    with session() as conn:
        for key, value in _DEFAULT_APP_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (key, value),
            )


def get_day_bounds() -> tuple[str, str]:
    """Returns (start "HH:MM", end "HH:MM"). Always returns a value —
    seed_app_settings() runs at every startup, same guarantee source_config
    rows have."""
    with session() as conn:
        rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('day_bounds_start', 'day_bounds_end')"
        ).fetchall()
    values = {row["key"]: row["value"] for row in rows}
    return (
        values.get("day_bounds_start", _DEFAULT_APP_SETTINGS["day_bounds_start"]),
        values.get("day_bounds_end", _DEFAULT_APP_SETTINGS["day_bounds_end"]),
    )


def set_day_bounds(start: str, end: str) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('day_bounds_start', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (start,),
        )
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('day_bounds_end', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (end,),
        )


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


def create_registered_project(name: str, path: str, description: str = "") -> int:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO registered_projects (name, path, description, active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (name, path, description, now),
        )
        return cursor.lastrowid


def list_registered_projects(active_only: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM registered_projects"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY name ASC"
    with session() as conn:
        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]


def get_registered_project(project_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM registered_projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row is not None else None


def update_registered_project(
    project_id: int,
    description: str | None = None,
    current_sprint: str | None = None,
    sprint_goal: str | None = None,
    blockers: str | None = None,
    target_date: str | None = None,
    active: bool | None = None,
) -> bool:
    """Partial update, same dynamic-SET pattern as
    update_recurring_commitment. Returns True if a row was matched."""
    sets: list[str] = []
    params: list[Any] = []
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if current_sprint is not None:
        sets.append("current_sprint = ?")
        params.append(current_sprint)
    if sprint_goal is not None:
        sets.append("sprint_goal = ?")
        params.append(sprint_goal)
    if blockers is not None:
        sets.append("blockers = ?")
        params.append(blockers)
    if target_date is not None:
        sets.append("target_date = ?")
        params.append(target_date)
    if active is not None:
        sets.append("active = ?")
        params.append(1 if active else 0)
    if not sets:
        return False
    params.append(project_id)
    with session() as conn:
        cursor = conn.execute(
            f"UPDATE registered_projects SET {', '.join(sets)} WHERE id = ?", params
        )
        return cursor.rowcount > 0


def delete_registered_project(project_id: int) -> bool:
    """Hard delete. Linked items keep their full history — only
    project_id is cleared, explicitly in application code rather than
    relying on SQLite's ALTER-added-column FK cascade behavior (which
    is version-dependent for columns added after table creation)."""
    with session() as conn:
        conn.execute("UPDATE items SET project_id = NULL WHERE project_id = ?", (project_id,))
        cursor = conn.execute("DELETE FROM registered_projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0


def create_finance_entry(amount: float, category: str, type_: str, entry_date: str, note: str = "") -> int:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO finance_entries (amount, category, note, entry_date, type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (amount, category, note, entry_date, type_, now),
        )
        return cursor.lastrowid


def list_finance_entries(start: str, end: str) -> list[dict[str, Any]]:
    """entry_date in [start, end), both ISO date strings — same half-open
    convention as history/aggregations.py. Newest first."""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM finance_entries WHERE entry_date >= ? AND entry_date < ? "
            "ORDER BY entry_date DESC, id DESC",
            (start, end),
        ).fetchall()
        return [dict(row) for row in rows]


def create_setup_invite(email: str, token: str, expires_at: str) -> int:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO setup_invites (email, token, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (email, token, now, expires_at),
        )
        return cursor.lastrowid


def get_setup_invite_by_token(token: str) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM setup_invites WHERE token = ?", (token,)).fetchone()
        return dict(row) if row is not None else None


def list_setup_invites() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM setup_invites ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]


def mark_setup_invite_used(token: str, used_by_ip: str | None) -> None:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        conn.execute(
            "UPDATE setup_invites SET used_at = ?, used_by_ip = ? WHERE token = ?",
            (now, used_by_ip, token),
        )
