import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from command_center import db
from command_center.config import TZ
from command_center.history import aggregations


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _today():
    return datetime.now(TZ).date()


def _add_event(task_id: int, event_type: str, timestamp: str, lane: str = "urgent", source: str = "gmail", title: str = "T") -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, event_type, timestamp, metadata_json) VALUES (?, ?, ?, ?)",
            (task_id, event_type, timestamp, json.dumps({"lane": lane, "source": source, "title": title})),
        )


# --- completion_rate -----------------------------------------------------


def test_completion_rate_counts_created_and_completed_in_range_by_lane(isolated_db: None) -> None:
    _add_event(1, "created", "2026-08-10T09:00:00+03:00", lane="urgent")
    _add_event(2, "created", "2026-08-11T09:00:00+03:00", lane="urgent")
    _add_event(3, "created", "2026-08-11T09:00:00+03:00", lane="reading")
    _add_event(1, "completed", "2026-08-12T09:00:00+03:00", lane="urgent")
    _add_event(2, "completed", "2026-08-12T09:00:00+03:00", lane="urgent")

    result = aggregations.completion_rate(date(2026, 8, 10), date(2026, 8, 17))

    assert result["total_created"] == 3
    assert result["total_completed"] == 2
    assert result["by_lane"]["urgent"] == {"created": 2, "completed": 2}
    assert result["by_lane"]["reading"] == {"created": 1, "completed": 0}


def test_completion_rate_excludes_events_outside_half_open_boundary(isolated_db: None) -> None:
    _add_event(1, "created", "2026-08-09T23:59:59+03:00")  # one second before start
    _add_event(2, "created", "2026-08-17T00:00:00+03:00")  # exactly at end (excluded)
    _add_event(3, "created", "2026-08-16T23:59:59+03:00")  # last instant included

    result = aggregations.completion_rate(date(2026, 8, 10), date(2026, 8, 17))

    assert result["total_created"] == 1


# --- same_day_resolution_rate ---------------------------------------------


def test_same_day_resolution_rate_computes_fraction_for_urgent_items(isolated_db: None) -> None:
    _add_event(1, "created", "2026-08-10T09:00:00+03:00", lane="urgent")
    _add_event(1, "completed", "2026-08-10T14:00:00+03:00", lane="urgent")
    _add_event(2, "created", "2026-08-11T09:00:00+03:00", lane="urgent")
    _add_event(2, "completed", "2026-08-13T09:00:00+03:00", lane="urgent")  # not same day

    rate = aggregations.same_day_resolution_rate(date(2026, 8, 10), date(2026, 8, 17))

    assert rate == 0.5


def test_same_day_resolution_rate_excludes_non_urgent_lanes(isolated_db: None) -> None:
    _add_event(1, "created", "2026-08-10T09:00:00+03:00", lane="reading")
    _add_event(1, "completed", "2026-08-10T14:00:00+03:00", lane="reading")

    rate = aggregations.same_day_resolution_rate(date(2026, 8, 10), date(2026, 8, 17))

    assert rate == 0.0


def test_same_day_resolution_rate_returns_zero_when_no_urgent_items(isolated_db: None) -> None:
    assert aggregations.same_day_resolution_rate(date(2026, 8, 10), date(2026, 8, 17)) == 0.0


# --- avg_time_to_complete --------------------------------------------------


def test_avg_time_to_complete_averages_hours_per_lane(isolated_db: None) -> None:
    _add_event(1, "created", "2026-08-10T08:00:00+03:00", lane="urgent")
    _add_event(1, "completed", "2026-08-10T10:00:00+03:00", lane="urgent")  # 2h
    _add_event(2, "created", "2026-08-10T08:00:00+03:00", lane="urgent")
    _add_event(2, "completed", "2026-08-10T12:00:00+03:00", lane="urgent")  # 4h

    result = aggregations.avg_time_to_complete(date(2026, 8, 10), date(2026, 8, 17))

    assert result["urgent"]["avg_hours"] == 3.0
    assert result["urgent"]["n"] == 2


def test_avg_time_to_complete_excludes_completions_with_no_created_event(isolated_db: None) -> None:
    _add_event(1, "completed", "2026-08-10T10:00:00+03:00", lane="urgent")  # orphan completion

    result = aggregations.avg_time_to_complete(date(2026, 8, 10), date(2026, 8, 17))

    assert result == {}


# --- rollover_count ---------------------------------------------------------


def test_rollover_count_counts_created_before_start_never_completed(isolated_db: None) -> None:
    _add_event(1, "created", "2026-08-01T09:00:00+03:00")
    assert aggregations.rollover_count(date(2026, 8, 10), date(2026, 8, 17)) == 1


def test_rollover_count_excludes_items_completed_before_end(isolated_db: None) -> None:
    _add_event(1, "created", "2026-08-01T09:00:00+03:00")
    _add_event(1, "completed", "2026-08-12T09:00:00+03:00")
    assert aggregations.rollover_count(date(2026, 8, 10), date(2026, 8, 17)) == 0


def test_rollover_count_includes_items_completed_after_end(isolated_db: None) -> None:
    _add_event(1, "created", "2026-08-01T09:00:00+03:00")
    _add_event(1, "completed", "2026-08-20T09:00:00+03:00")  # after period end
    assert aggregations.rollover_count(date(2026, 8, 10), date(2026, 8, 17)) == 1


def test_rollover_count_excludes_items_created_after_start(isolated_db: None) -> None:
    _add_event(1, "created", "2026-08-12T09:00:00+03:00")
    assert aggregations.rollover_count(date(2026, 8, 10), date(2026, 8, 17)) == 0


def test_rollover_count_counts_completed_then_reopened_before_end(isolated_db: None) -> None:
    _add_event(1, "created", "2026-08-01T09:00:00+03:00")
    _add_event(1, "completed", "2026-08-05T09:00:00+03:00")
    _add_event(1, "reopened", "2026-08-12T09:00:00+03:00")
    assert aggregations.rollover_count(date(2026, 8, 10), date(2026, 8, 17)) == 1


# --- completion_trend --------------------------------------------------------


def test_completion_trend_zero_fills_quiet_buckets(isolated_db: None) -> None:
    _add_event(1, "completed", "2026-08-12T09:00:00+03:00")

    trend = aggregations.completion_trend(date(2026, 8, 10), date(2026, 8, 17), "day")

    assert len(trend) == 7
    counts = {b["bucket"]: b["count"] for b in trend}
    assert counts["2026-08-12"] == 1
    assert counts["2026-08-10"] == 0
    assert counts["2026-08-16"] == 0


# --- current_streak -----------------------------------------------------


def test_current_streak_counts_consecutive_days_including_today(isolated_db: None) -> None:
    today = _today()
    _add_event(1, "completed", f"{today.isoformat()}T09:00:00+03:00")
    _add_event(2, "completed", f"{(today - timedelta(days=1)).isoformat()}T09:00:00+03:00")
    _add_event(3, "completed", f"{(today - timedelta(days=2)).isoformat()}T09:00:00+03:00")

    assert aggregations.current_streak() == 3


def test_current_streak_counts_from_yesterday_when_today_is_empty(isolated_db: None) -> None:
    today = _today()
    _add_event(1, "completed", f"{(today - timedelta(days=1)).isoformat()}T09:00:00+03:00")
    _add_event(2, "completed", f"{(today - timedelta(days=2)).isoformat()}T09:00:00+03:00")

    assert aggregations.current_streak() == 2


def test_current_streak_zero_when_today_and_yesterday_both_empty(isolated_db: None) -> None:
    today = _today()
    _add_event(1, "completed", f"{(today - timedelta(days=5)).isoformat()}T09:00:00+03:00")

    assert aggregations.current_streak() == 0


def test_current_streak_zero_with_no_history(isolated_db: None) -> None:
    assert aggregations.current_streak() == 0


def test_current_streak_stops_at_a_gap(isolated_db: None) -> None:
    today = _today()
    _add_event(1, "completed", f"{today.isoformat()}T09:00:00+03:00")
    # gap at yesterday
    _add_event(2, "completed", f"{(today - timedelta(days=2)).isoformat()}T09:00:00+03:00")

    assert aggregations.current_streak() == 1


# --- all_time_completed --------------------------------------------------


def test_all_time_completed_sums_across_all_history(isolated_db: None) -> None:
    _add_event(1, "completed", "2020-01-01T09:00:00+03:00")
    _add_event(2, "completed", "2026-08-12T09:00:00+03:00")
    _add_event(3, "created", "2026-08-12T09:00:00+03:00")  # not completed, not counted

    assert aggregations.all_time_completed() == 2


def test_all_time_completed_zero_with_no_history(isolated_db: None) -> None:
    assert aggregations.all_time_completed() == 0


# --- best_period -----------------------------------------------------------


def test_best_period_returns_none_for_day(isolated_db: None) -> None:
    _add_event(1, "completed", "2026-08-12T09:00:00+03:00")
    assert aggregations.best_period("day") is None


def test_best_period_returns_none_with_no_history(isolated_db: None) -> None:
    assert aggregations.best_period("week") is None


def test_best_period_finds_highest_count_week(isolated_db: None) -> None:
    # Week of 2026-08-10 (Monday) gets 2 completions; week of 2026-08-17 gets 1.
    _add_event(1, "completed", "2026-08-10T09:00:00+03:00")
    _add_event(2, "completed", "2026-08-11T09:00:00+03:00")
    _add_event(3, "completed", "2026-08-17T09:00:00+03:00")

    result = aggregations.best_period("week")

    assert result["count"] == 2
    assert result["label"] == "Week of August 10–16, 2026"


def test_best_period_finds_highest_count_month(isolated_db: None) -> None:
    _add_event(1, "completed", "2026-08-01T09:00:00+03:00")
    _add_event(2, "completed", "2026-08-15T09:00:00+03:00")
    _add_event(3, "completed", "2026-09-01T09:00:00+03:00")

    result = aggregations.best_period("month")

    assert result["count"] == 2
    assert result["label"] == "August 2026"


def test_best_period_finds_highest_count_year(isolated_db: None) -> None:
    _add_event(1, "completed", "2025-01-01T09:00:00+03:00")
    _add_event(2, "completed", "2026-08-15T09:00:00+03:00")
    _add_event(3, "completed", "2026-09-01T09:00:00+03:00")

    result = aggregations.best_period("year")

    assert result["count"] == 2
    assert result["label"] == "2026"


# --- period_over_period_delta ------------------------------------------------


def test_period_over_period_delta_compares_to_immediately_preceding_week(isolated_db: None) -> None:
    _add_event(1, "completed", "2026-08-10T09:00:00+03:00")  # current week
    _add_event(2, "completed", "2026-08-11T09:00:00+03:00")  # current week
    _add_event(3, "completed", "2026-08-03T09:00:00+03:00")  # previous week

    result = aggregations.period_over_period_delta("week", date(2026, 8, 10), date(2026, 8, 17))

    assert result == {"current": 2, "previous": 1, "delta": 1}


def test_period_over_period_delta_zero_activity(isolated_db: None) -> None:
    result = aggregations.period_over_period_delta("week", date(2026, 8, 10), date(2026, 8, 17))
    assert result == {"current": 0, "previous": 0, "delta": 0}


def test_period_over_period_delta_negative_when_current_is_lower(isolated_db: None) -> None:
    _add_event(1, "completed", "2026-08-10T09:00:00+03:00")  # current week
    _add_event(2, "completed", "2026-08-03T09:00:00+03:00")  # previous week
    _add_event(3, "completed", "2026-08-04T09:00:00+03:00")  # previous week

    result = aggregations.period_over_period_delta("week", date(2026, 8, 10), date(2026, 8, 17))

    assert result == {"current": 1, "previous": 2, "delta": -1}
