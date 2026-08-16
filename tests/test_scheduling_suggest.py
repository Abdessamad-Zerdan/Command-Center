from datetime import datetime, timedelta
from pathlib import Path

import pytest

from command_center import db, queries
from command_center.config import TZ
from command_center.scheduling import suggest


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    queries.seed_app_settings()


def _today():
    return datetime.now(TZ).date()


def _far_week_start():
    """A Monday well outside 'today's' week, so every day in it is
    low-confidence (calendar_checked() is only ever true for real
    today) — keeps ordering/slot tests independent of the real date."""
    d = _today() + timedelta(days=30)
    return d - timedelta(days=d.weekday())


def _this_week_start():
    return _today() - timedelta(days=_today().weekday())


# --- ordering ----------------------------------------------------------------


def test_suggest_placements_orders_by_lane_then_priority_then_due_date(isolated_db: None) -> None:
    week_start = _far_week_start()
    # Deliberately created out of priority order, to prove sorting (not
    # insertion order) drives placement.
    reading_id = queries.create_manual_item(week_start.isoformat(), "reading", "Read article")
    urgent_id = queries.create_manual_item(week_start.isoformat(), "urgent", "Urgent task")
    action_id = queries.create_manual_item(week_start.isoformat(), "action_items", "Action task")

    placements = suggest.suggest_placements(week_start)

    ordered_ids = [p["item_id"] for p in placements]
    assert ordered_ids.index(urgent_id) < ordered_ids.index(action_id) < ordered_ids.index(reading_id)


def test_suggest_placements_orders_by_priority_within_same_lane(isolated_db: None) -> None:
    week_start = _far_week_start()
    low_id = queries.create_manual_item(week_start.isoformat(), "urgent", "Low priority")
    high_id = queries.create_manual_item(week_start.isoformat(), "urgent", "High priority")
    with db.session() as conn:
        conn.execute("UPDATE items SET priority = 3 WHERE id = ?", (low_id,))
        conn.execute("UPDATE items SET priority = 1 WHERE id = ?", (high_id,))

    placements = suggest.suggest_placements(week_start)

    ordered_ids = [p["item_id"] for p in placements]
    assert ordered_ids.index(high_id) < ordered_ids.index(low_id)


def test_suggest_placements_orders_earliest_due_date_first_within_lane(isolated_db: None) -> None:
    week_start = _far_week_start()
    later_id = queries.create_manual_item(
        week_start.isoformat(), "urgent", "Due later", due_date=(week_start + timedelta(days=5)).isoformat()
    )
    earlier_id = queries.create_manual_item(
        week_start.isoformat(), "urgent", "Due sooner", due_date=week_start.isoformat()
    )
    no_due_id = queries.create_manual_item(week_start.isoformat(), "urgent", "No due date")

    placements = suggest.suggest_placements(week_start)

    ordered_ids = [p["item_id"] for p in placements]
    assert ordered_ids.index(earlier_id) < ordered_ids.index(later_id) < ordered_ids.index(no_due_id)


# --- one task per slot, no double-booking ------------------------------------


def test_suggest_placements_never_assigns_two_tasks_the_same_slot(isolated_db: None) -> None:
    week_start = _far_week_start()
    for i in range(5):
        queries.create_manual_item(week_start.isoformat(), "urgent", f"Task {i}")

    placements = suggest.suggest_placements(week_start)

    seen = set()
    for p in placements:
        key = (p["proposed_date"], p["proposed_start"])
        assert key not in seen
        seen.add(key)


# --- low_confidence flag ------------------------------------------------------


def test_suggest_placements_flags_non_today_days_as_low_confidence(isolated_db: None) -> None:
    week_start = _far_week_start()
    queries.create_manual_item(week_start.isoformat(), "urgent", "Some task")

    placements = suggest.suggest_placements(week_start)

    assert len(placements) == 1
    assert placements[0]["low_confidence"] is True


def test_suggest_placements_does_not_flag_today_as_low_confidence(isolated_db: None) -> None:
    week_start = _this_week_start()
    today_iso = _today().isoformat()
    queries.create_manual_item(today_iso, "urgent", "Today's task", due_date=today_iso)

    placements = suggest.suggest_placements(week_start)

    todays_placements = [p for p in placements if p["proposed_date"] == today_iso]
    assert len(todays_placements) >= 1
    assert all(p["low_confidence"] is False for p in todays_placements)


# --- estimated duration --------------------------------------------------------


def test_suggest_placements_uses_default_30_minute_estimate(isolated_db: None) -> None:
    week_start = _far_week_start()
    queries.create_manual_item(week_start.isoformat(), "urgent", "Some task")

    placements = suggest.suggest_placements(week_start)

    assert placements[0]["duration_minutes"] == 30
    assert placements[0]["estimated_duration"] is True


# --- empty-input edge cases ----------------------------------------------------


def test_suggest_placements_no_unscheduled_tasks_returns_empty(isolated_db: None) -> None:
    assert suggest.suggest_placements(_far_week_start()) == []


def test_suggest_placements_no_free_slots_returns_empty(isolated_db: None) -> None:
    week_start = _far_week_start()
    queries.create_manual_item(week_start.isoformat(), "urgent", "Some task")
    # A recurring commitment spanning the full default day bounds, every
    # day of the week, leaves zero free time to place anything into.
    for day in range(7):
        queries.create_recurring_commitment("All day", day, "07:00", "22:00")

    assert suggest.suggest_placements(week_start) == []


def test_suggest_placements_task_too_large_for_remaining_slots_is_omitted(isolated_db: None) -> None:
    week_start = _far_week_start()
    # Leave only a 15-minute gap each day — smaller than the 30-minute
    # default estimate — by bracketing it with two recurring commitments.
    for day in range(7):
        queries.create_recurring_commitment("Morning", day, "07:00", "12:00")
        queries.create_recurring_commitment("Afternoon", day, "12:15", "22:00")
    queries.create_manual_item(week_start.isoformat(), "urgent", "Too big to fit")

    placements = suggest.suggest_placements(week_start)

    assert placements == []  # omitted silently, not an error
