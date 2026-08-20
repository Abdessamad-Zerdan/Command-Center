"""Read/write helpers used by the routes. Thin wrappers around sqlite3.Row."""

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from command_center.config import LANE_LABELS, LANES, TZ
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
            "ORDER BY priority ASC, sort_order ASC, id ASC",
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
    manual ones (triage can be wrong). Returns True if a row was updated.

    A move away from the lane the LLM originally assigned (source is
    anything but 'manual' — a manual item was never triaged, so moving
    it isn't a correction) also logs a triage_corrections row, feeding
    /settings/triage-rules' "recent corrections" panel.
    """
    with session() as conn:
        row = conn.execute(
            "SELECT lane, source, title, brief_date FROM items WHERE id = ?", (item_id,)
        ).fetchone()
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
            if row["source"] != "manual" and row["lane"] != lane:
                conn.execute(
                    "INSERT INTO triage_corrections "
                    "(item_id, brief_date, source, title, from_lane, to_lane, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item_id, row["brief_date"], row["source"], row["title"],
                        row["lane"], lane, datetime.now(TZ).isoformat(),
                    ),
                )
        return updated


def list_triage_corrections(limit: int = 20) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM triage_corrections ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def triage_correction_patterns(min_count: int = 2) -> list[dict[str, Any]]:
    """Groups corrections by (source, from_lane, to_lane) — a repeated
    pattern (moved the same way `min_count`+ times) is a real signal a
    triage rule would fix, not a one-off. Ordered by count desc so the
    strongest pattern surfaces first."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT source, from_lane, to_lane, COUNT(*) AS count,
                   MAX(title) AS example_title
            FROM triage_corrections
            GROUP BY source, from_lane, to_lane
            HAVING COUNT(*) >= ?
            ORDER BY count DESC
            """,
            (min_count,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_stale_pending_items(lane: str, cutoff_iso: str) -> list[dict[str, Any]]:
    """Pending items in `lane` created at or before `cutoff_iso` — the
    dashboard's "stale urgent" nudge uses created_at as the only signal
    for "hasn't been touched," since nothing else in the schema tracks
    a last-viewed time."""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE lane = ? AND status = 'pending' AND created_at <= ? "
            "ORDER BY created_at ASC",
            (lane, cutoff_iso),
        ).fetchall()
        return [dict(row) for row in rows]


def list_overdue_tasks(today: str) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE lane = 'tasks_due' AND status = 'pending' "
            "AND due_date IS NOT NULL AND due_date < ? ORDER BY due_date ASC",
            (today,),
        ).fetchall()
        return [dict(row) for row in rows]


def reorder_item(item_id: int, after_item_id: int | None) -> bool:
    """Moves item_id to sit immediately after after_item_id (or first in
    the lane, if after_item_id is None) among the *other pending items
    sharing its lane and brief_date* — scoped across the whole lane, not
    just item_id's own priority band. An earlier version scoped this to
    same-priority siblings only, on the theory that a drag should never
    silently jump an item across the High/Medium/Low sub-headers the UI
    groups by — in practice that meant most real drags (lanes routinely
    span more than one priority) 404'd invisibly, since the fetch()
    caller doesn't surface a failed reorder. Dropping onto an item in a
    different band now adopts that item's priority too, so the dragged
    item actually lands wherever it's dropped, sub-header included.
    Returns False if item_id doesn't exist, or after_item_id isn't
    actually a sibling in the same lane — a no-op, not an error, since a
    stale drag target is the only realistic way to hit this.

    Re-spaces every sibling to clean, evenly-spaced values (in their
    current, already-correct display order — priority ASC, then
    sort_order) before computing the new item's midpoint. Cheap for a
    lane-sized group, and it's what makes the midpoint always
    well-defined — every row shares the same sort_order=0 default until
    the first-ever reorder in a group, so a naive midpoint between two
    still-equal values wouldn't move anything; re-spacing first
    guarantees the two neighbors are never equal.
    """
    with session() as conn:
        item = conn.execute(
            "SELECT lane, priority, brief_date FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if item is None:
            return False

        siblings = conn.execute(
            "SELECT id, priority FROM items "
            "WHERE lane = ? AND brief_date = ? AND status = 'pending' AND id != ? "
            "ORDER BY priority ASC, sort_order ASC, id ASC",
            (item["lane"], item["brief_date"], item_id),
        ).fetchall()

        if after_item_id is not None and not any(s["id"] == after_item_id for s in siblings):
            return False

        spaced = []
        for i, sibling in enumerate(siblings):
            new_val = float(i * 10)
            conn.execute("UPDATE items SET sort_order = ? WHERE id = ?", (new_val, sibling["id"]))
            spaced.append({"id": sibling["id"], "sort_order": new_val, "priority": sibling["priority"]})

        if after_item_id is None:
            next_order = spaced[0]["sort_order"] if spaced else 0.0
            new_order = next_order - 10.0
            new_priority = item["priority"]
        else:
            idx = next(i for i, s in enumerate(spaced) if s["id"] == after_item_id)
            after_order = spaced[idx]["sort_order"]
            next_order = spaced[idx + 1]["sort_order"] if idx + 1 < len(spaced) else after_order + 20.0
            new_order = (after_order + next_order) / 2.0
            new_priority = spaced[idx]["priority"]

        conn.execute(
            "UPDATE items SET sort_order = ?, priority = ? WHERE id = ?",
            (new_order, new_priority, item_id),
        )
        return True


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


def get_item_for_calendar(item_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute(
            "SELECT id, title, source, due_date, scheduled_start, scheduled_end, "
            "calendar_event_id, calendar_link FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None


def set_item_calendar_event(item_id: int, event_id: str, link: str) -> bool:
    """Records a real Google Calendar event created for this item — a
    non-empty calendar_event_id is what the UI checks to switch the
    "Add to Calendar" button to a "View in Calendar" link instead."""
    with session() as conn:
        row = conn.execute("SELECT lane, source, title FROM items WHERE id = ?", (item_id,)).fetchone()
        cursor = conn.execute(
            "UPDATE items SET calendar_event_id = ?, calendar_link = ? WHERE id = ?",
            (event_id, link, item_id),
        )
        updated = cursor.rowcount > 0
        if updated and row is not None:
            log_task_event(
                item_id, "updated",
                metadata={
                    "lane": row["lane"], "source": row["source"], "title": row["title"],
                    "calendar_event_id": event_id,
                },
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


def list_tool_calls(limit: int = 50) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM tool_call_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def list_task_events(limit: int = 50) -> list[dict[str, Any]]:
    """task_id is a soft reference (see db.py's SCHEMA) — the row it
    pointed at may since have been mutated, replaced, or deleted, so
    metadata_json (captured at the moment of the event) is the only
    reliable source for what it was about, not a join back to items."""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM task_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def list_triage_rules(enabled_only: bool = False) -> list[dict[str, Any]]:
    """Oldest first — the order rules were created in is also the order
    triage_rules.py applies them (first match wins), so this is the
    order a user needs to see them in to understand which one would win
    a conflict."""
    with session() as conn:
        sql = "SELECT * FROM triage_rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id ASC"
        rows = conn.execute(sql).fetchall()
        return [dict(row) for row in rows]


def create_triage_rule(field: str, match_value: str, lane: str) -> int:
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO triage_rules (field, match_value, lane, enabled, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (field, match_value, lane, datetime.now(TZ).isoformat()),
        )
        return cursor.lastrowid


def set_triage_rule_enabled(rule_id: int, enabled: bool) -> bool:
    with session() as conn:
        cursor = conn.execute(
            "UPDATE triage_rules SET enabled = ? WHERE id = ?", (1 if enabled else 0, rule_id)
        )
        return cursor.rowcount > 0


def delete_triage_rule(rule_id: int) -> bool:
    with session() as conn:
        cursor = conn.execute("DELETE FROM triage_rules WHERE id = ?", (rule_id,))
        return cursor.rowcount > 0


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


def get_lane_labels() -> dict[str, str]:
    """config.LANE_LABELS (from profile.py) as the default, overlaid with
    any per-lane renames saved via /settings/lane-labels. Renaming a
    lane's *display text* never touches the lane slugs themselves
    (config.LANES) — triage, the DB `lane` column, and chat tool schemas
    all keep using urgent/action_items/etc. regardless."""
    labels = dict(LANE_LABELS)
    with session() as conn:
        rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE key LIKE 'lane_label_%'"
        ).fetchall()
    for row in rows:
        lane = row["key"].removeprefix("lane_label_")
        if lane in labels:
            labels[lane] = row["value"]
    return labels


def set_lane_label(lane: str, label: str) -> bool:
    """Blank `label` clears the override, reverting to config.LANE_LABELS'
    default rather than saving an empty string. Returns False for an
    unknown lane."""
    if lane not in LANES:
        return False
    key = f"lane_label_{lane}"
    with session() as conn:
        if not label.strip():
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, label.strip()),
            )
    return True


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


def was_nudge_notified_today(nudge_id: str, today: str) -> bool:
    """One ntfy push per nudge id per day — the coordinator tick runs
    every 5 minutes, so without this a still-unresolved nudge would
    spam a push every tick instead of once when it first appears."""
    with session() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (f"ntfy_last_sent_{nudge_id}",)
        ).fetchone()
    return row is not None and row["value"] == today


def mark_nudge_notified(nudge_id: str, today: str) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"ntfy_last_sent_{nudge_id}", today),
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
        # id DESC as a tiebreaker — two invites created back-to-back can
        # get the exact same created_at timestamp string (seen in
        # practice, not just theoretical), and created_at DESC alone
        # leaves SQLite's tie-break order undefined, which showed up as
        # a real intermittent test failure. id always reflects true
        # insertion order regardless.
        rows = conn.execute(
            "SELECT * FROM setup_invites ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def mark_setup_invite_used(token: str, used_by_ip: str | None) -> None:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        conn.execute(
            "UPDATE setup_invites SET used_at = ?, used_by_ip = ? WHERE token = ?",
            (now, used_by_ip, token),
        )


# --- data export --------------------------------------------------------------
# Plain full-table dumps for Settings → Export data. No filtering, no
# pagination — this is "your data, not the repo's" (same framing as the
# gitignored files), meant to leave with you if you ever stop running
# this instance, not a paginated API for routine use.


def export_items() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM items ORDER BY brief_date ASC, id ASC").fetchall()
        return [dict(row) for row in rows]


def export_finance_entries() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM finance_entries ORDER BY entry_date ASC, id ASC").fetchall()
        return [dict(row) for row in rows]


def export_briefs() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM briefs ORDER BY brief_date ASC").fetchall()
        return [dict(row) for row in rows]


# --- knowledge_documents -------------------------------------------------------
# Uploaded CVs/project docs the assistant's index draws from, alongside
# vision.md and profile.py — see assistant/documents.py for extraction.


def create_knowledge_document(filename: str, stored_name: str, char_count: int) -> int:
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO knowledge_documents (filename, stored_name, char_count, uploaded_at) "
            "VALUES (?, ?, ?, ?)",
            (filename, stored_name, char_count, datetime.now(TZ).isoformat()),
        )
        return cursor.lastrowid


def list_knowledge_documents() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM knowledge_documents ORDER BY uploaded_at DESC, id DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def get_knowledge_document(doc_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM knowledge_documents WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None


def delete_knowledge_document(doc_id: int) -> bool:
    with session() as conn:
        cursor = conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (doc_id,))
        return cursor.rowcount > 0
