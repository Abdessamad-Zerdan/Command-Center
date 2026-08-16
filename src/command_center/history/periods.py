"""Pure period-boundary math for /history/report — day/week/month/year
interval computation, prev/next navigation, and human labels. Zero
dependencies on router.py, aggregations.py, or reflection.py, so any of
those three can import this module freely: router.py needs it for the
GET route's own bounds, and reflection.py needs it to compute the
*previous* period's bounds for comparison — since router.py also needs
to call reflection.py, reflection.py importing router.py back would be
circular, so this shared math lives here instead, dependency-free.

Raises plain ValueError on bad input (no FastAPI import here at all) —
router.py's call sites catch ValueError and re-raise HTTPException(400),
so the route's observable behavior is unchanged from when these lived
there directly.
"""

from datetime import date as date_type
from datetime import datetime, timedelta

from command_center.config import TZ

PERIODS = ("day", "week", "month", "year")


def parse_anchor(date_str: str | None) -> date_type:
    if date_str is None:
        return datetime.now(TZ).date()
    try:
        return date_type.fromisoformat(date_str)
    except ValueError:
        raise ValueError(f"Invalid date: {date_str!r}")


def period_bounds(period: str, anchor: date_type) -> tuple[date_type, date_type]:
    """[start, end) — end is the first day NOT in the period."""
    if period == "day":
        return anchor, anchor + timedelta(days=1)
    if period == "week":
        start = anchor - timedelta(days=anchor.weekday())  # Monday, ISO week
        return start, start + timedelta(days=7)
    if period == "month":
        start = anchor.replace(day=1)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
        return start, end
    if period == "year":
        start = anchor.replace(month=1, day=1)
        return start, start.replace(year=start.year + 1)
    raise ValueError(f"Unknown period: {period!r}")


def shift_anchor(period: str, anchor: date_type, direction: int) -> date_type:
    """direction: -1 previous, +1 next. Returns a date INSIDE the adjacent
    period; period_bounds re-derives that period's own start/end from it."""
    start, end = period_bounds(period, anchor)
    if period == "day":
        return anchor + timedelta(days=direction)
    if period == "week":
        return anchor + timedelta(days=7 * direction)
    if period == "month":
        return end if direction > 0 else start - timedelta(days=1)
    if period == "year":
        return anchor.replace(year=anchor.year + direction)
    raise ValueError(f"Unknown period: {period!r}")


def label(period: str, start: date_type, end: date_type) -> str:
    # Deliberately not using strftime's "%-d" (strip-leading-zero) —
    # that's a glibc/macOS extension, not supported by Python's strftime
    # on Windows, so it would raise ValueError there. Built manually
    # instead so this works cross-platform.
    end_incl = end - timedelta(days=1)
    if period == "day":
        return f"{start.strftime('%A, %B')} {start.day}, {start.year}"
    if period == "week":
        if start.month == end_incl.month:
            return f"Week of {start.strftime('%B')} {start.day}–{end_incl.day}, {end_incl.year}"
        return (
            f"Week of {start.strftime('%b')} {start.day} – "
            f"{end_incl.strftime('%b')} {end_incl.day}, {end_incl.year}"
        )
    if period == "month":
        return start.strftime("%B %Y")
    return str(start.year)
