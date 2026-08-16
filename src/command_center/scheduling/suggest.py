"""Deterministic placement suggestions — no LLM call, a greedy
assignment over free_slots_for_week()'s output. Never writes to items;
scheduling/router.py's accept-placement route is the only write path,
gated behind explicit user confirmation, same principle as every other
write-action in this app.
"""

from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any

from command_center import queries
from command_center.config import LANES, TZ
from command_center.scheduling import availability

_DEFAULT_DURATION_MINUTES = 30
_NULL_DUE_DATE_SENTINEL = "9999-12-31"  # sorts after every real ISO date string


def _sort_key(task: dict[str, Any]) -> tuple[int, int, str]:
    lane_rank = LANES.index(task["lane"]) if task["lane"] in LANES else len(LANES)
    return (lane_rank, task["priority"], task["due_date"] or _NULL_DUE_DATE_SENTINEL)


def suggest_placements(week_start: date_type) -> list[dict[str, Any]]:
    """One dict per proposed placement — never writes anything. Candidate
    tasks are scanned in priority order (lane rank, then priority, then
    due_date earliest-first with NULLs last); each is greedily matched to
    the first free slot (in whole-week chronological order) it fits in.
    A matched slot is fully consumed even if larger than the task's
    estimate — one task per free slot per pass, no splitting/packing.
    Days where calendar data isn't real (every day but today — see
    availability.calendar_checked) still get suggestions, just flagged
    low_confidence rather than excluded, so 6 of 7 days aren't silently
    skipped.
    """
    # Own inclusive week_end (Sunday), distinct from history/periods.py's
    # half-open convention used elsewhere in this app — this one's only
    # purpose is the due-date eligibility filter below, not slot math.
    week_end = week_start + timedelta(days=6)
    tasks = queries.list_unscheduled_task_candidates(week_end.isoformat())
    tasks.sort(key=_sort_key)

    # A requested week can span days already in the past (e.g. "this
    # week" viewed on a Saturday still includes Monday-Friday) — a
    # placement suggestion for a slot that's already elapsed would be
    # unusable, and a bad placement is worse than no placement, so past
    # days are excluded from the candidate pool entirely.
    today = datetime.now(TZ).date()
    week_slots = availability.free_slots_for_week(week_start)
    candidates = [
        (d, slot_start, slot_end)
        for d in sorted(week_slots)
        if d >= today
        for slot_start, slot_end in week_slots[d]
    ]
    used = [False] * len(candidates)

    placements: list[dict[str, Any]] = []
    for task in tasks:
        for i, (slot_date, slot_start, slot_end) in enumerate(candidates):
            if used[i]:
                continue
            slot_minutes = (
                datetime.combine(slot_date, slot_end) - datetime.combine(slot_date, slot_start)
            ).total_seconds() / 60
            if slot_minutes < _DEFAULT_DURATION_MINUTES:
                continue
            used[i] = True  # whole slot claimed, even if it has leftover capacity
            start_dt = datetime.combine(slot_date, slot_start)
            end_dt = start_dt + timedelta(minutes=_DEFAULT_DURATION_MINUTES)
            placements.append(
                {
                    "item_id": task["id"],
                    "title": task["title"],
                    "lane": task["lane"],
                    "proposed_date": slot_date.isoformat(),
                    "proposed_start": start_dt.strftime("%H:%M"),
                    "proposed_end": end_dt.strftime("%H:%M"),
                    "duration_minutes": _DEFAULT_DURATION_MINUTES,
                    "estimated_duration": True,
                    "low_confidence": not availability.calendar_checked(slot_date),
                }
            )
            break
        # else: no slot fit this pass — task silently omitted, not an error.
    return placements
