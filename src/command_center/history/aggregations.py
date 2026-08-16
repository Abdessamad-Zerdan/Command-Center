"""Read-only aggregation queries over the append-only `task_events` log
(see queries.py::log_task_event). All functions take `start`/`end` as a
half-open interval [start, end) — `end` is the first day NOT included —
and filter by plain ISO8601 string comparison, same convention used
everywhere else in this codebase's date handling.

Lane attribution: every task is bucketed by the lane recorded in its own
`created` event's metadata_json, never by items.lane (which can drift
after the fact via update_item_lane) and never by a later event's lane.
This is what makes a past period's report permanently reproducible —
moving a task to a different lane today doesn't retroactively change how
last week's report reads. Where no `created` event exists (pre-migration
items, or a force=True re-pull of an already-known item — see
save_triage_results), lane resolves to a synthetic "unknown" bucket via
COALESCE rather than being silently dropped, so counts always reconcile.
"""

from datetime import date, datetime, timedelta
from typing import Any

from command_center.config import TZ
from command_center.db import session
from command_center.history import periods


def _bounds(start: date, end: date) -> tuple[str, str]:
    return start.isoformat(), end.isoformat()


def completion_rate(start: date, end: date) -> dict[str, Any]:
    """{'by_lane': {lane: {'created': int, 'completed': int}},
    'total_created': int, 'total_completed': int}"""
    start_iso, end_iso = _bounds(start, end)
    with session() as conn:
        created_rows = conn.execute(
            """
            SELECT COALESCE(json_extract(metadata_json, '$.lane'), 'unknown') AS lane, COUNT(*) AS n
            FROM task_events
            WHERE event_type = 'created' AND timestamp >= ? AND timestamp < ?
            GROUP BY lane
            """,
            (start_iso, end_iso),
        ).fetchall()
        completed_rows = conn.execute(
            """
            SELECT COALESCE(json_extract(c.metadata_json, '$.lane'), 'unknown') AS lane, COUNT(*) AS n
            FROM task_events e
            LEFT JOIN task_events c ON c.task_id = e.task_id AND c.event_type = 'created'
            WHERE e.event_type = 'completed' AND e.timestamp >= ? AND e.timestamp < ?
            GROUP BY lane
            """,
            (start_iso, end_iso),
        ).fetchall()

    by_lane: dict[str, dict[str, int]] = {}
    for row in created_rows:
        by_lane.setdefault(row["lane"], {"created": 0, "completed": 0})["created"] = row["n"]
    for row in completed_rows:
        by_lane.setdefault(row["lane"], {"created": 0, "completed": 0})["completed"] = row["n"]

    return {
        "by_lane": by_lane,
        "total_created": sum(v["created"] for v in by_lane.values()),
        "total_completed": sum(v["completed"] for v in by_lane.values()),
    }


def same_day_resolution_rate(start: date, end: date) -> float:
    """Of urgent-lane items created in [start, end), the fraction whose
    earliest 'completed' event shares the same calendar day (compared via
    the timestamp's 'YYYY-MM-DD' prefix). Returns 0.0, never raises, if no
    urgent items were created in range."""
    start_iso, end_iso = _bounds(start, end)
    with session() as conn:
        created_rows = conn.execute(
            """
            SELECT task_id, timestamp AS created_ts
            FROM task_events
            WHERE event_type = 'created'
              AND json_extract(metadata_json, '$.lane') = 'urgent'
              AND timestamp >= ? AND timestamp < ?
            """,
            (start_iso, end_iso),
        ).fetchall()
        if not created_rows:
            return 0.0

        task_ids = [r["task_id"] for r in created_rows]
        placeholders = ",".join("?" * len(task_ids))
        completed_rows = conn.execute(
            f"""
            SELECT task_id, MIN(timestamp) AS completed_ts
            FROM task_events
            WHERE event_type = 'completed' AND task_id IN ({placeholders})
            GROUP BY task_id
            """,
            task_ids,
        ).fetchall()

    completed_by_task = {r["task_id"]: r["completed_ts"] for r in completed_rows}
    same_day = sum(
        1
        for r in created_rows
        if (ts := completed_by_task.get(r["task_id"])) and ts[:10] == r["created_ts"][:10]
    )
    return same_day / len(created_rows)


def avg_time_to_complete(start: date, end: date) -> dict[str, dict[str, float | int]]:
    """{lane: {'avg_hours': float, 'n': int}} for tasks *completed* in
    [start, end). Tasks with no recorded 'created' event are excluded
    entirely (not bucketed as 'unknown') since there's no valid duration
    to compute for them."""
    start_iso, end_iso = _bounds(start, end)
    with session() as conn:
        rows = conn.execute(
            """
            SELECT
              COALESCE(json_extract(c.metadata_json, '$.lane'), 'unknown') AS lane,
              c.timestamp AS created_ts,
              e.timestamp AS completed_ts
            FROM task_events e
            LEFT JOIN task_events c ON c.task_id = e.task_id AND c.event_type = 'created'
            WHERE e.event_type = 'completed' AND e.timestamp >= ? AND e.timestamp < ?
            """,
            (start_iso, end_iso),
        ).fetchall()

    hours_by_lane: dict[str, list[float]] = {}
    for row in rows:
        if row["created_ts"] is None:
            continue
        delta = datetime.fromisoformat(row["completed_ts"]) - datetime.fromisoformat(row["created_ts"])
        hours_by_lane.setdefault(row["lane"], []).append(delta.total_seconds() / 3600)

    return {
        lane: {"avg_hours": sum(vals) / len(vals), "n": len(vals)}
        for lane, vals in hours_by_lane.items()
    }


def rollover_count(start: date, end: date) -> int:
    """Items created before `start` whose most recent event strictly
    before `end` is anything other than 'completed' — i.e. still open at
    period end. Uses each task_id's latest event (not just presence/
    absence of a 'completed' row) so a task completed after `end`, or
    completed-then-reopened, is still correctly counted as open through
    this period."""
    start_iso, end_iso = _bounds(start, end)
    with session() as conn:
        row = conn.execute(
            """
            WITH created_before AS (
                SELECT DISTINCT task_id FROM task_events
                WHERE event_type = 'created' AND timestamp < ?
            ),
            ranked AS (
                SELECT te.task_id, te.event_type,
                       ROW_NUMBER() OVER (
                           PARTITION BY te.task_id ORDER BY te.timestamp DESC, te.id DESC
                       ) AS rn
                FROM task_events te
                JOIN created_before cb ON cb.task_id = te.task_id
                WHERE te.timestamp < ?
            )
            SELECT COUNT(*) AS n FROM ranked WHERE rn = 1 AND event_type != 'completed'
            """,
            (start_iso, end_iso),
        ).fetchone()
    return row["n"]


_GRANULARITY_SLICE_LEN = {"hour": 13, "day": 10, "month": 7}  # matches timestamp prefix width


def _bucket_keys(start: date, end: date, granularity: str) -> list[str]:
    if granularity == "hour":
        return [f"{start.isoformat()}T{h:02d}" for h in range(24)]
    if granularity == "day":
        keys, d = [], start
        while d < end:
            keys.append(d.isoformat())
            d += timedelta(days=1)
        return keys
    if granularity == "month":
        keys, y, m = [], start.year, start.month
        while date(y, m, 1) < end:
            keys.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return keys
    raise ValueError(f"Unknown granularity: {granularity!r}")


def completion_trend(start: date, end: date, granularity: str) -> list[dict[str, Any]]:
    """[{'bucket': str, 'count': int}, ...] in chronological order,
    zero-filled across every bucket in [start, end) — a bucket with no
    completions must appear as count=0, not be omitted, so the trend
    line's X spacing stays uniform."""
    start_iso, end_iso = _bounds(start, end)
    slice_len = _GRANULARITY_SLICE_LEN[granularity]
    with session() as conn:
        rows = conn.execute(
            f"""
            SELECT substr(timestamp, 1, {slice_len}) AS bucket, COUNT(*) AS n
            FROM task_events
            WHERE event_type = 'completed' AND timestamp >= ? AND timestamp < ?
            GROUP BY bucket
            """,
            (start_iso, end_iso),
        ).fetchall()
    counts = {r["bucket"]: r["n"] for r in rows}
    return [{"bucket": b, "count": counts.get(b, 0)} for b in _bucket_keys(start, end, granularity)]


def current_streak() -> int:
    """Consecutive days (walking backward from today) with >=1 completed
    task. If today has no completions yet, that doesn't reset the streak
    — today isn't over — it's checked against yesterday instead. Returns
    0 only when the chain is already broken (today AND yesterday both
    have zero)."""
    today = datetime.now(TZ).date()
    with session() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(timestamp, 1, 10) AS day FROM task_events WHERE event_type = 'completed'"
        ).fetchall()
    completed_days = {row["day"] for row in rows}

    if today.isoformat() in completed_days:
        cursor = today
    elif (today - timedelta(days=1)).isoformat() in completed_days:
        cursor = today - timedelta(days=1)
    else:
        return 0

    streak = 0
    while cursor.isoformat() in completed_days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def all_time_completed() -> int:
    with session() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM task_events WHERE event_type = 'completed'").fetchone()
    return row["n"]


def best_period(period_type: str) -> dict[str, Any] | None:
    """{"label": str, "count": int} for the historical period (of the
    given type) with the highest completed count. Not meaningful at day
    granularity — returns None, and the sidebar simply omits this block
    for Day view (same skip-for-Day pattern the Reflection card already
    uses)."""
    if period_type == "day":
        return None
    with session() as conn:
        rows = conn.execute(
            "SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS n "
            "FROM task_events WHERE event_type = 'completed' GROUP BY day"
        ).fetchall()
    if not rows:
        return None

    bucket_counts: dict[str, int] = {}
    bucket_anchor: dict[str, date] = {}
    for row in rows:
        d = date.fromisoformat(row["day"])
        if period_type == "week":
            key = (d - timedelta(days=d.weekday())).isoformat()
        elif period_type == "month":
            key = f"{d.year:04d}-{d.month:02d}"
        elif period_type == "year":
            key = str(d.year)
        else:
            raise ValueError(f"Unknown period_type: {period_type!r}")
        bucket_counts[key] = bucket_counts.get(key, 0) + row["n"]
        bucket_anchor.setdefault(key, d)

    best_key = max(bucket_counts, key=bucket_counts.get)
    p_start, p_end = periods.period_bounds(period_type, bucket_anchor[best_key])
    return {"label": periods.label(period_type, p_start, p_end), "count": bucket_counts[best_key]}


def period_over_period_delta(period_type: str, current_start: date, current_end: date) -> dict[str, int]:
    """{"current": int, "previous": int, "delta": int} — completed count
    this period vs. the immediately preceding period of the same type,
    using the same "day before current_start, re-derive bounds" anchoring
    reflection.py already uses for its own previous-period comparison."""
    prev_start, prev_end = periods.period_bounds(period_type, current_start - timedelta(days=1))
    current_start_iso, current_end_iso = _bounds(current_start, current_end)
    prev_start_iso, prev_end_iso = _bounds(prev_start, prev_end)
    with session() as conn:
        current_n = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE event_type='completed' AND timestamp >= ? AND timestamp < ?",
            (current_start_iso, current_end_iso),
        ).fetchone()["n"]
        previous_n = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE event_type='completed' AND timestamp >= ? AND timestamp < ?",
            (prev_start_iso, prev_end_iso),
        ).fetchone()["n"]
    return {"current": current_n, "previous": previous_n, "delta": current_n - previous_n}
