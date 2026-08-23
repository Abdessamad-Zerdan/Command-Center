"""Read-only aggregations over daily_log/training_session/running_log for
/fitness. Windows are 7-day rolling windows anchored on `end_date`, not
calendar weeks — matches the exact "last 7 days" / "this 7-day window vs
the previous one" language the dashboard's status chips use throughout.

This module only ever compares logged numbers against targets already
sitting in fitness_settings — it never derives or suggests a target
itself. That split (queries.py owns raw reads/writes, this module owns
"what does that mean") mirrors finances/aggregations.py.
"""

from datetime import date, timedelta
from typing import Any

from command_center import queries


def trailing_window(end_date: date, days: int = 7) -> tuple[str, str]:
    """[start, end) half-open ISO strings for the `days`-day window
    ending on (and including) end_date."""
    start = end_date - timedelta(days=days - 1)
    end_exclusive = end_date + timedelta(days=1)
    return start.isoformat(), end_exclusive.isoformat()


def protein_status(end_date: date, target_g: float) -> dict[str, Any]:
    """Counts, over the trailing 7 calendar days ending end_date, how
    many days a logged protein_g met or exceeded target_g — a day with
    no log at all, or logged below target, counts as not met. Amber
    only when the most recent 2 days *both* missed: a single off day
    isn't worth flagging."""
    start, end = trailing_window(end_date, 7)
    logs = {log["log_date"]: log for log in queries.list_daily_logs(start, end)}
    days = [(end_date - timedelta(days=i)).isoformat() for i in range(7)]

    def _met(d: str) -> bool:
        log = logs.get(d)
        return bool(log and (log["protein_g"] or 0) >= target_g)

    met_days = sum(1 for d in days if _met(d))
    missed_recent_two = not any(_met(d) for d in days[:2])
    return {
        "met_days": met_days,
        "total_days": 7,
        "status": "amber" if missed_recent_two else "green",
    }


def surplus_addon_status(end_date: date) -> dict[str, Any]:
    """Same shape as protein_status — a day "counts" if at least one
    configured add-on was checked that day."""
    start, end = trailing_window(end_date, 7)
    logs = {log["log_date"]: log for log in queries.list_daily_logs(start, end)}
    days = [(end_date - timedelta(days=i)).isoformat() for i in range(7)]

    def _logged(d: str) -> bool:
        log = logs.get(d)
        return bool(log and log["addons_checked"])

    logged_days = sum(1 for d in days if _logged(d))
    missed_recent_two = not any(_logged(d) for d in days[:2])
    return {
        "logged_days": logged_days,
        "total_days": 7,
        "status": "amber" if missed_recent_two else "green",
    }


def training_volume_status(end_date: date) -> dict[str, Any]:
    """This 7-day window's total resistance volume vs the previous
    7-day window. pct_change is None when the previous window had zero
    volume to compare against — "+inf%"/"-100%" isn't an honest
    percentage, so the template falls back to a plain "new volume"
    label in that case instead."""
    this_start, this_end = trailing_window(end_date, 7)
    prev_end_date = end_date - timedelta(days=7)
    prev_start, prev_end = trailing_window(prev_end_date, 7)

    this_volume = queries.total_training_volume(this_start, this_end)
    prev_volume = queries.total_training_volume(prev_start, prev_end)

    pct_change = None
    if prev_volume > 0:
        pct_change = (this_volume - prev_volume) / prev_volume * 100

    status = "amber" if (prev_volume > 0 and this_volume < prev_volume) else "green"

    return {
        "this_week": this_volume,
        "last_week": prev_volume,
        "pct_change": pct_change,
        "status": status,
    }


def running_status(end_date: date, cap_km: float | None) -> dict[str, Any]:
    """Purely a heads-up when trailing-7-day distance exceeds cap_km —
    extra mileage works against a surplus, so this flags it without any
    imperative language. cap_km is often None (the spec seeds it blank,
    left for the user to fill in) — no cap means never amber."""
    start, end = trailing_window(end_date, 7)
    distance = queries.total_running_distance(start, end)
    over_cap = cap_km is not None and distance > cap_km
    return {
        "distance_km": distance,
        "cap_km": cap_km,
        "status": "amber" if over_cap else "green",
    }


def headline_stats(end_date: date, settings: dict[str, Any]) -> dict[str, Any]:
    """The four small stat tiles at the top of the dashboard."""
    week_start, week_end = trailing_window(end_date, 7)
    current_weight = settings["current_weight"]
    # Positive = kg still needed to reach the low end of the goal band
    # (the seed data is a gain-weight goal); zero or negative means
    # already at or past it.
    gap_to_goal_low = settings["goal_weight_low"] - current_weight
    return {
        "current_weight": current_weight,
        "gap_to_goal_low": gap_to_goal_low,
        "sessions_this_week": queries.count_training_sessions(week_start, week_end),
        "sessions_target": settings["resistance_sessions_per_week_target"],
        "runs_this_week": queries.count_running_logs(week_start, week_end),
        "runs_cap": settings["running_sessions_per_week_cap"],
    }


def weekly_volume_series(end_date: date, num_weeks: int = 8) -> list[dict[str, Any]]:
    """[{'bucket': 'Aug 10', 'count': float}, ...] — total resistance
    volume per trailing 7-day window, oldest first, ending at (and
    including) end_date's own window. 'count' matches the key
    fitness/charts.py's render_bars_svg expects."""
    out = []
    for i in range(num_weeks - 1, -1, -1):
        window_end = end_date - timedelta(days=7 * i)
        start, end = trailing_window(window_end, 7)
        out.append({"bucket": window_end.strftime("%b %d"), "count": queries.total_training_volume(start, end)})
    return out


def weekly_running_series(end_date: date, num_weeks: int = 8) -> list[dict[str, Any]]:
    """Same shape as weekly_volume_series, for weekly running distance."""
    out = []
    for i in range(num_weeks - 1, -1, -1):
        window_end = end_date - timedelta(days=7 * i)
        start, end = trailing_window(window_end, 7)
        out.append({"bucket": window_end.strftime("%b %d"), "count": queries.total_running_distance(start, end)})
    return out
