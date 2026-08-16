"""Read-only aggregation queries over `finance_entries` for /finances.
All functions take `start`/`end` as a half-open interval [start, end) —
`end` is the first day NOT included — same convention as
history/aggregations.py.
"""

from datetime import date
from typing import Any

from command_center.db import session
from command_center.history import periods


def monthly_summary(start: date, end: date) -> dict[str, float]:
    """{'total_spent': float, 'total_income': float, 'total_saved': float,
    'net_delta': float} — net_delta = total_income - total_spent (savings
    is tracked separately, not subtracted from net)."""
    start_iso, end_iso = start.isoformat(), end.isoformat()
    with session() as conn:
        rows = conn.execute(
            "SELECT type, COALESCE(SUM(amount), 0) AS total FROM finance_entries "
            "WHERE entry_date >= ? AND entry_date < ? GROUP BY type",
            (start_iso, end_iso),
        ).fetchall()
    totals = {row["type"]: row["total"] for row in rows}
    total_spent = totals.get("spend", 0.0)
    total_income = totals.get("income", 0.0)
    total_saved = totals.get("saving", 0.0)
    return {
        "total_spent": total_spent,
        "total_income": total_income,
        "total_saved": total_saved,
        "net_delta": total_income - total_spent,
    }


def category_breakdown(start: date, end: date) -> dict[str, float]:
    """{category: total} for type='spend' entries only — income/saving
    are already surfaced in monthly_summary's own cards, so mixing all
    three types into one bar chart would conflate inflows and outflows
    under one axis."""
    start_iso, end_iso = start.isoformat(), end.isoformat()
    with session() as conn:
        rows = conn.execute(
            "SELECT category, COALESCE(SUM(amount), 0) AS total FROM finance_entries "
            "WHERE type = 'spend' AND entry_date >= ? AND entry_date < ? GROUP BY category",
            (start_iso, end_iso),
        ).fetchall()
    return {row["category"]: row["total"] for row in rows}


def _trailing_month_starts(anchor_month_start: date, num_months: int) -> list[date]:
    """`anchor_month_start` must already be a month's day-1 start (as
    returned by periods.period_bounds("month", ...)). Walks backward via
    shift_anchor/period_bounds exactly like periods.py's own prev/next
    navigation, so this can never drift from that module's month math
    (leap years, 30 vs 31 day months, year rollover)."""
    starts = [anchor_month_start]
    cursor = anchor_month_start
    for _ in range(num_months - 1):
        prev_inside = periods.shift_anchor("month", cursor, -1)
        cursor, _ = periods.period_bounds("month", prev_inside)
        starts.append(cursor)
    starts.reverse()
    return starts


def monthly_trend(month_start: date, num_months: int = 6) -> list[dict[str, Any]]:
    """[{'bucket': 'Mar', 'count': float}, ...] — total spent per month,
    zero-filled, for the trailing `num_months` months ending at (and
    including) `month_start`'s own month. 'count' is a money total, not
    a count — named to match the shape charts.render_trend_svg expects.
    """
    starts = _trailing_month_starts(month_start, num_months)
    range_start = starts[0]
    range_end = periods.period_bounds("month", starts[-1])[1]
    with session() as conn:
        rows = conn.execute(
            "SELECT substr(entry_date, 1, 7) AS bucket, COALESCE(SUM(amount), 0) AS total "
            "FROM finance_entries WHERE type = 'spend' AND entry_date >= ? AND entry_date < ? "
            "GROUP BY bucket",
            (range_start.isoformat(), range_end.isoformat()),
        ).fetchall()
    totals = {row["bucket"]: row["total"] for row in rows}
    return [
        {
            "bucket": s.strftime("%b"),
            "count": totals.get(f"{s.year:04d}-{s.month:02d}", 0.0),
        }
        for s in starts
    ]
