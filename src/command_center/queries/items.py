"""Brief items (tasks, mails, events on a day): status, lanes, dates, links, attachments.

Part of the queries package — see queries/__init__.py."""

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from command_center.config import TZ
from command_center.db import session


# No caller passes "pending" to set_item_status today (no reopen/un-snooze
# endpoint exists yet), but mapping it now means a future reopen feature
# needs zero changes here.
_STATUS_EVENT_MAP = {"done": "completed", "snoozed": "snoozed", "dismissed": "dismissed", "pending": "reopened"}



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



def wake_snoozed_items(today: str) -> int:
    """Brings back every task snoozed until `today` or earlier: pending
    again, on today's brief, first in its priority group (sort_order -1;
    reorder re-spaces from 0 up). Returns how many woke.

    Waits for today's brief row to exist on purpose — creating it here
    would make startup think today's pull already ran and skip it. A
    snooze with no date (from before "Later" had one) stays asleep: it
    has no day to come back on."""
    with session() as conn:
        if conn.execute("SELECT 1 FROM briefs WHERE brief_date = ?", (today,)).fetchone() is None:
            return 0
        rows = conn.execute(
            "SELECT id, lane, source, title FROM items "
            "WHERE status = 'snoozed' AND snoozed_until IS NOT NULL AND snoozed_until <= ?",
            (today,),
        ).fetchall()
        for row in rows:
            # snoozed_until is kept: the card reads it to say "Back from Later".
            conn.execute(
                "UPDATE items SET status = 'pending', brief_date = ?, sort_order = -1 WHERE id = ?",
                (today, row["id"]),
            )
            log_task_event(
                row["id"],
                "reopened",
                metadata={"lane": row["lane"], "source": row["source"], "title": row["title"], "from": "later"},
                conn=conn,
            )
        return len(rows)



def move_item_to_date(item_id: int, new_brief_date: str, lane: str | None = None) -> bool:
    """Moves an item to a different day's brief — resets status to
    'pending' and created_at to now (so it actually shows up there, and
    stops immediately re-tripping the stale-urgent nudge, whose only
    signal for "untouched" is created_at) and optionally changes lane
    in the same update. Ensures the target day's briefs row exists
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

        sets = ["brief_date = ?", "status = 'pending'", "created_at = ?"]
        params: list[Any] = [new_brief_date, now]
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



def set_item_due_date(item_id: int, due_date: str | None) -> bool:
    """Drag-and-drop on /calendar's month grid — moves an item to a
    different due date (or clears it, due_date=None) without touching
    lane/brief_date/status, unlike move_item_to_date (which is for
    bringing a whole history-day item back into today's active brief).
    Returns True if a row was updated."""
    with session() as conn:
        cursor = conn.execute("UPDATE items SET due_date = ? WHERE id = ?", (due_date, item_id))
        return cursor.rowcount > 0



def list_pending_calendar_synced_items() -> list[dict[str, Any]]:
    """Open tasks the app pushed to Google Calendar — the ones whose event
    the user may have moved or renamed there since (calendar_sync)."""
    with session() as conn:
        rows = conn.execute(
            "SELECT id, title, source, due_date, calendar_event_id FROM items "
            "WHERE status = 'pending' AND source != 'calendar' "
            "AND calendar_event_id IS NOT NULL AND calendar_event_id != ''"
        ).fetchall()
        return [dict(row) for row in rows]


def list_pending_items_by_due_date(start: str, end: str) -> list[dict[str, Any]]:
    """Pending items with due_date in [start, end) — /calendar's task
    layer. Scoped by due_date alone, not lane or brief_date: due_date is
    a standalone field any pending item can carry regardless of which
    day it was triaged into, and "what's coming" should surface every
    one of them."""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE status = 'pending' AND due_date >= ? AND due_date < ? "
            "ORDER BY due_date ASC, priority ASC, id ASC",
            (start, end),
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

        # Landing in another priority band is the user choosing it — lock
        # it so prioritize.py never moves it back.
        conn.execute(
            "UPDATE items SET sort_order = ?, priority = ?, "
            "priority_locked = CASE WHEN ? != priority THEN 1 ELSE priority_locked END WHERE id = ?",
            (new_order, new_priority, new_priority, item_id),
        )
        return True


def set_item_priority(item_id: int, priority: int) -> bool:
    """The user putting a task in High/Medium/Low themselves (the priority
    board). Locked from then on — prioritize.py never overrides it."""
    with session() as conn:
        cursor = conn.execute(
            "UPDATE items SET priority = ?, priority_locked = 1 WHERE id = ?", (priority, item_id)
        )
        return cursor.rowcount > 0


def list_tasks_to_prioritize(today: str, oldest: str) -> list[dict[str, Any]]:
    """Open work the brief shows today (its own day plus carried-over days
    back to `oldest`) in the action lanes, minus anything whose priority
    the user set themselves."""
    with session() as conn:
        rows = conn.execute(
            "SELECT id, title, lane, source, due_date, deadline, why_it_matters, priority FROM items "
            "WHERE status = 'pending' AND priority_locked = 0 "
            "AND lane IN ('urgent', 'action_items', 'tasks_due') "
            "AND brief_date BETWEEN ? AND ? ORDER BY id",
            (oldest, today),
        ).fetchall()
        return [dict(row) for row in rows]


def save_model_priorities(updates: list[tuple[int, int, str]]) -> int:
    """(item_id, priority, reason) from prioritize.py. Skips any row the
    user locked in the meantime. Returns how many rows changed."""
    changed = 0
    with session() as conn:
        for item_id, priority, reason in updates:
            cursor = conn.execute(
                "UPDATE items SET priority = ?, priority_reason = ? WHERE id = ? AND priority_locked = 0",
                (priority, reason, item_id),
            )
            changed += cursor.rowcount
    return changed



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



def set_item_map_node(item_id: int, map_node_id: int | None) -> bool:
    with session() as conn:
        cursor = conn.execute("UPDATE items SET map_node_id = ? WHERE id = ?", (map_node_id, item_id))
        return cursor.rowcount > 0



def set_item_pursuit(item_id: int, pursuit_id: int | None) -> bool:
    with session() as conn:
        cursor = conn.execute("UPDATE items SET pursuit_id = ? WHERE id = ?", (pursuit_id, item_id))
        return cursor.rowcount > 0



def list_items_by_pursuit(pursuit_id: int) -> list[dict[str, Any]]:
    """Every task linked to a research topic / target, open ones first."""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE pursuit_id = ? "
            "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, brief_date DESC, id DESC",
            (pursuit_id,),
        ).fetchall()
        return [dict(row) for row in rows]



# --- data export --------------------------------------------------------------
# Plain full-table dumps for Settings → Export data. No filtering, no
# pagination — this is "your data, not the repo's" (same framing as the
# gitignored files), meant to leave with you if you ever stop running
# this instance, not a paginated API for routine use.


def export_items() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM items ORDER BY brief_date ASC, id ASC").fetchall()
        return [dict(row) for row in rows]



def item_exists(item_id: int) -> bool:
    with session() as conn:
        return conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is not None



def create_item_attachment(
    item_id: int, stored_name: str, original_name: str, content_type: str, size_bytes: int
) -> int:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO item_attachments (item_id, stored_name, original_name, content_type, size_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, stored_name, original_name, content_type, size_bytes, now),
        )
        return cursor.lastrowid



def list_item_attachments(item_id: int) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM item_attachments WHERE item_id = ? ORDER BY id ASC", (item_id,)
        ).fetchall()
        return [dict(row) for row in rows]



def get_item_attachment(attachment_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM item_attachments WHERE id = ?", (attachment_id,)).fetchone()
        return dict(row) if row else None



def delete_item_attachment(attachment_id: int) -> bool:
    with session() as conn:
        cursor = conn.execute("DELETE FROM item_attachments WHERE id = ?", (attachment_id,))
        return cursor.rowcount > 0



def get_attachment_counts(item_ids: list[int]) -> dict[int, int]:
    """{item_id: photo count} — same shape and "absent means zero"
    convention as get_time_logged_by_source_ids."""
    if not item_ids:
        return {}
    with session() as conn:
        placeholders = ",".join("?" for _ in item_ids)
        rows = conn.execute(
            f"SELECT item_id, COUNT(*) AS n FROM item_attachments "
            f"WHERE item_id IN ({placeholders}) GROUP BY item_id",
            item_ids,
        ).fetchall()
        return {row["item_id"]: row["n"] for row in rows}



def get_item(item_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None



def set_item_competition(item_id: int, competition_id: int | None) -> bool:
    with session() as conn:
        cursor = conn.execute("UPDATE items SET competition_id = ? WHERE id = ?", (competition_id, item_id))
        return cursor.rowcount > 0



def list_items_by_competition(competition_id: int) -> list[dict[str, Any]]:
    """Every task linked to a competition, open ones first."""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE competition_id = ? "
            "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, brief_date DESC, id DESC",
            (competition_id,),
        ).fetchall()
        return [dict(row) for row in rows]
