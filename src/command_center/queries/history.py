"""Briefs: saving a pull, reading a day, history dates, CSV export of briefs.

Part of the queries package — see queries/__init__.py."""

import json
from datetime import date, datetime, timedelta
from typing import Any

from command_center.config import LANES, TZ
from command_center.db import session
from command_center.queries.items import log_task_event


def latest_brief_date_before(brief_date: str) -> str | None:
    """The most recent brief older than `brief_date` — what /brief shows
    while today's first pull is still running in the background."""
    with session() as conn:
        row = conn.execute(
            "SELECT MAX(brief_date) AS d FROM briefs WHERE brief_date < ?", (brief_date,)
        ).fetchone()
        return row["d"] if row else None



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



def list_recent_picked_reading(oldest: str, before: str) -> list[dict[str, Any]]:
    """Still-unread Reading picks (priority 1–2, chosen by reading.pick)
    from `oldest` up to but not including `before` — the few articles
    worth carrying a couple of days, unlike the rest of Reading, which
    list_carried_over leaves out."""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE status = 'pending' AND lane = 'reading' AND priority <= 2 "
            "AND brief_date >= ? AND brief_date < ? ORDER BY brief_date DESC, priority ASC, id ASC",
            (oldest, before),
        ).fetchall()
        return [dict(row) for row in rows]


def list_carried_over(brief_date: str, days: int) -> list[dict[str, Any]]:
    """Pending items still open from the `days` days before `brief_date`,
    newest day first. Reading is left out on purpose — it's skim-later
    material, not work, and would bury the real tasks.

    Calendar items only come from yesterday: a pull fetches today's and
    tomorrow's events, and the item stores no event time of its own, so
    anything older is an event that has already happened."""
    day = date.fromisoformat(brief_date)
    start = (day - timedelta(days=days)).isoformat()
    yesterday = (day - timedelta(days=1)).isoformat()
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE status = 'pending' AND lane != 'reading' "
            "AND brief_date >= ? AND brief_date < ? "
            "AND (source != 'calendar' OR brief_date >= ?) "
            "ORDER BY brief_date DESC, priority ASC, sort_order ASC, id ASC",
            (start, brief_date, yesterday),
        ).fetchall()
        return [dict(row) for row in rows]



def list_history_dates() -> list[str]:
    with session() as conn:
        rows = conn.execute(
            "SELECT brief_date FROM briefs ORDER BY brief_date DESC"
        ).fetchall()
        return [row["brief_date"] for row in rows]



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
        _upsert_brief_row(conn, brief_date, now, degraded_sources, sources_attempted)
        insert_verb = "INSERT OR REPLACE" if force else "INSERT OR IGNORE"
        for item in triaged_items:
            _save_triaged_item(conn, brief_date, item, now, insert_verb)
        if "calendar" in sources_attempted:
            _replace_calendar_events(conn, brief_date, calendar_events)



def _upsert_brief_row(
    conn, brief_date: str, now: str, degraded_sources: list[str], sources_attempted: list[str]
) -> None:
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



def _save_triaged_item(conn, brief_date: str, item: dict[str, Any], now: str, insert_verb: str) -> None:
    existing = conn.execute(
        "SELECT id FROM items WHERE source = ? AND source_id = ?",
        (item["source"], item["source_id"]),
    ).fetchone()
    cursor = conn.execute(
        f"""
        {insert_verb} INTO items
            (brief_date, lane, source, source_id, title, why_it_matters,
             suggested_next_step, priority, deep_link, due_date, source_time, deadline, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
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
            item.get("source_time"),
            item.get("deadline"),
            now,
        ),
    )
    if item.get("source_time") and existing is not None:
        # INSERT OR IGNORE leaves an already-known item untouched —
        # fill in its source time if it was saved before we kept one.
        conn.execute(
            "UPDATE items SET source_time = ? WHERE source = ? AND source_id = ? AND source_time IS NULL",
            (item["source_time"], item["source"], item["source_id"]),
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



def _replace_calendar_events(conn, brief_date: str, calendar_events: list[dict[str, Any]]) -> None:
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



def export_briefs() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM briefs ORDER BY brief_date ASC").fetchall()
        return [dict(row) for row in rows]
