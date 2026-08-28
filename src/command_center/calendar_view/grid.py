"""Pure month-grid date math for /calendar — no I/O, kept separate from
router.py so it's trivial to unit test without touching Google auth or
the database.
"""

from datetime import date, timedelta


def month_bounds(month_start: date) -> tuple[date, date]:
    """[start, end) for month_start's own calendar month. month_start
    must already be day=1 — callers normalize before calling this."""
    if month_start.month == 12:
        end = date(month_start.year + 1, 1, 1)
    else:
        end = date(month_start.year, month_start.month + 1, 1)
    return month_start, end


def build_weeks(month_start: date) -> list[list[date]]:
    """Monday-start weeks fully covering month_start's month, padded
    with the leading/trailing days from adjacent months a real calendar
    grid needs to stay rectangular (4-6 weeks depending on the month)."""
    _, month_end = month_bounds(month_start)
    grid_start = month_start - timedelta(days=month_start.weekday())
    last_day = month_end - timedelta(days=1)
    grid_end = last_day + timedelta(days=(6 - last_day.weekday()) + 1)  # exclusive

    weeks = []
    cursor = grid_start
    while cursor < grid_end:
        weeks.append([cursor + timedelta(days=i) for i in range(7)])
        cursor += timedelta(days=7)
    return weeks


def shift_month(month_start: date, delta: int) -> date:
    """delta=+1/-1 (or any integer) month offset, always landing on
    day=1 — used for the prev/next month links."""
    zero_based_month = month_start.month - 1 + delta
    year = month_start.year + zero_based_month // 12
    month = zero_based_month % 12 + 1
    return date(year, month, 1)
