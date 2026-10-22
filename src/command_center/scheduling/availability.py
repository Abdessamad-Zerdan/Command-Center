"""Free-time computation: day bounds, minus active recurring commitments,
minus today's ingested calendar events. Pure functions except for the
queries.py reads (day bounds, recurring commitments) and one direct
calendar_events query — no network calls, no writes.

Calendar-data honesty: calendar_events only ever holds *today's* ingested
events. sources/calendar.py's CalendarSource.fetch_with_events() fetches a
2-day window and buckets events into today_events/tomorrow_events, but
only today_events becomes the timeline_rows that reach calendar_events —
tomorrow_events only flows into the items table as an LLM-triaged
suggestion, with no structured start/end retained. So calendar_events
never contains tomorrow's events, or any date beyond today, ever — not
an edge case, a deterministic zero. free_slots() for any date still
correctly subtracts recurring_commitments; it just can't subtract
calendar events for a non-today date because none exist. Callers that
need to show this distinction to a user (the /schedule route) use
calendar_checked(), not free_slots() directly — free_slots()'s return
type stays exactly as specified, this is a separate, single-purpose
signal.
"""

from datetime import date as date_type
from datetime import datetime, time, timedelta

from command_center import queries
from command_center.config import TZ
from command_center.db import session

Interval = tuple[time, time]


def _parse_hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _day_bounds_for(d: date_type) -> tuple[datetime, datetime]:
    start_str, end_str = queries.get_day_bounds()
    start = datetime.combine(d, _parse_hhmm(start_str), tzinfo=TZ)
    end = datetime.combine(d, _parse_hhmm(end_str), tzinfo=TZ)
    return start, end


def _recurring_busy_intervals(d: date_type) -> list[tuple[datetime, datetime]]:
    rows = queries.list_recurring_commitments(day_of_week=d.weekday(), active_only=True)
    intervals = []
    for row in rows:
        start = datetime.combine(d, _parse_hhmm(row["start_time"]), tzinfo=TZ)
        end = datetime.combine(d, _parse_hhmm(row["end_time"]), tzinfo=TZ)
        if end > start:
            intervals.append((start, end))
    return intervals


def _calendar_busy_intervals(d: date_type) -> list[tuple[datetime, datetime]]:
    """Only returns data for d == today (see module docstring). Any other
    date returns [] — not because the day is free, but because there is
    nothing ingested to check."""
    if d != datetime.now(TZ).date():
        return []
    with session() as conn:
        rows = conn.execute(
            "SELECT start_time, end_time FROM calendar_events WHERE brief_date = ?",
            (d.isoformat(),),
        ).fetchall()
    intervals = []
    for row in rows:
        start_raw, end_raw = row["start_time"], row["end_time"]
        # All-day Google events store a bare "YYYY-MM-DD" (no "T") — a
        # whole-day marker, not a real busy time range. Treating it as
        # busy would zero out the entire free-slot day, a much bigger
        # behavior change than this pass should make silently — so
        # all-day events are simply excluded from the busy set.
        if "T" not in start_raw or "T" not in end_raw:
            continue
        start_dt = datetime.fromisoformat(start_raw)
        end_dt = datetime.fromisoformat(end_raw)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=TZ)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=TZ)
        if start_dt.astimezone(TZ).date() != d:
            continue
        intervals.append((start_dt.astimezone(TZ), end_dt.astimezone(TZ)))
    return intervals


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # overlapping or touching -> merge
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _complement(
    busy: list[tuple[datetime, datetime]], bound_start: datetime, bound_end: datetime
) -> list[Interval]:
    free: list[Interval] = []
    cursor = bound_start
    for busy_start, busy_end in busy:
        clipped_start = max(busy_start, bound_start)
        clipped_end = min(busy_end, bound_end)
        if clipped_start > cursor:
            free.append((cursor.timetz().replace(tzinfo=None), clipped_start.timetz().replace(tzinfo=None)))
        cursor = max(cursor, clipped_end)
    if cursor < bound_end:
        free.append((cursor.timetz().replace(tzinfo=None), bound_end.timetz().replace(tzinfo=None)))
    return [(s, e) for s, e in free if s < e]


def free_slots(d: date_type) -> list[Interval]:
    """Free time on date d, as a list of (start_time, end_time) within
    the configured day bounds — day bounds minus every active recurring
    commitment for d.weekday() minus today's ingested calendar events
    (if d is today; no calendar subtraction for any other date)."""
    bound_start, bound_end = _day_bounds_for(d)
    busy = _recurring_busy_intervals(d) + _calendar_busy_intervals(d)
    merged = _merge_intervals(busy)
    return _complement(merged, bound_start, bound_end)


def calendar_checked(d: date_type) -> bool:
    """True only for today — the one date free_slots() can actually
    subtract real calendar data for."""
    return d == datetime.now(TZ).date()


def free_slots_for_week(start_date: date_type) -> dict[date_type, list[Interval]]:
    return {start_date + timedelta(days=i): free_slots(start_date + timedelta(days=i)) for i in range(7)}
