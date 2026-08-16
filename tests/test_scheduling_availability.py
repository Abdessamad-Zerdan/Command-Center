import json
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from command_center import db, queries
from command_center.config import TZ
from command_center.scheduling import availability


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    queries.seed_app_settings()


def _today():
    return datetime.now(TZ).date()


def _add_calendar_event(brief_date: str, start_time: str, end_time: str) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO briefs (brief_date, generated_at, degraded_lanes) VALUES (?, ?, '[]')",
            (brief_date, datetime.now(TZ).isoformat()),
        )
        conn.execute(
            "INSERT INTO calendar_events (brief_date, title, start_time, end_time, attendees, link) "
            "VALUES (?, 'Event', ?, ?, '[]', NULL)",
            (brief_date, start_time, end_time),
        )


# --- free_slots: day bounds + recurring commitments -----------------------


def test_free_slots_empty_day_returns_full_day_bounds(isolated_db: None) -> None:
    d = _today() + timedelta(days=10)  # far from today, avoids any calendar interaction

    slots = availability.free_slots(d)

    assert slots == [(time(7, 0), time(22, 0))]


def test_free_slots_subtracts_active_recurring_commitment(isolated_db: None) -> None:
    d = _today() + timedelta(days=10)
    queries.create_recurring_commitment("Work hours", d.weekday(), "09:00", "17:00")

    slots = availability.free_slots(d)

    assert slots == [(time(7, 0), time(9, 0)), (time(17, 0), time(22, 0))]


def test_free_slots_ignores_inactive_recurring_commitment(isolated_db: None) -> None:
    d = _today() + timedelta(days=10)
    commitment_id = queries.create_recurring_commitment("Work hours", d.weekday(), "09:00", "17:00")
    queries.update_recurring_commitment(commitment_id, active=False)

    slots = availability.free_slots(d)

    assert slots == [(time(7, 0), time(22, 0))]


def test_free_slots_ignores_commitment_on_different_day_of_week(isolated_db: None) -> None:
    d = _today() + timedelta(days=10)
    other_day = (d.weekday() + 1) % 7
    queries.create_recurring_commitment("Gym", other_day, "09:00", "17:00")

    slots = availability.free_slots(d)

    assert slots == [(time(7, 0), time(22, 0))]


def test_free_slots_merges_overlapping_commitments(isolated_db: None) -> None:
    d = _today() + timedelta(days=10)
    queries.create_recurring_commitment("A", d.weekday(), "09:00", "13:00")
    queries.create_recurring_commitment("B", d.weekday(), "12:30", "17:00")

    slots = availability.free_slots(d)

    assert slots == [(time(7, 0), time(9, 0)), (time(17, 0), time(22, 0))]


def test_free_slots_merges_touching_commitments(isolated_db: None) -> None:
    d = _today() + timedelta(days=10)
    queries.create_recurring_commitment("Gym", d.weekday(), "18:00", "19:00")
    queries.create_recurring_commitment("Sync", d.weekday(), "19:00", "19:30")

    slots = availability.free_slots(d)

    assert slots == [(time(7, 0), time(18, 0)), (time(19, 30), time(22, 0))]


def test_free_slots_respects_custom_day_bounds(isolated_db: None) -> None:
    queries.set_day_bounds("06:00", "23:00")
    d = _today() + timedelta(days=10)

    slots = availability.free_slots(d)

    assert slots == [(time(6, 0), time(23, 0))]


# --- free_slots: calendar events (today only) ------------------------------


def test_free_slots_subtracts_todays_calendar_events(isolated_db: None) -> None:
    today = _today()
    today_iso = today.isoformat()
    start = datetime.combine(today, time(10, 0), tzinfo=TZ).isoformat()
    end = datetime.combine(today, time(11, 0), tzinfo=TZ).isoformat()
    _add_calendar_event(today_iso, start, end)

    slots = availability.free_slots(today)

    assert (time(10, 0), time(11, 0)) not in slots
    assert (time(7, 0), time(10, 0)) in slots
    assert (time(11, 0), time(22, 0)) in slots


def test_free_slots_excludes_all_day_bare_date_calendar_events(isolated_db: None) -> None:
    today = _today()
    today_iso = today.isoformat()
    _add_calendar_event(today_iso, today_iso, today_iso)  # bare date, no "T" — all-day event

    slots = availability.free_slots(today)

    assert slots == [(time(7, 0), time(22, 0))]


def test_free_slots_does_not_subtract_calendar_events_for_non_today_date(isolated_db: None) -> None:
    today = _today()
    tomorrow = today + timedelta(days=1)
    # Simulates the real ingestion shape: a calendar_events row exists
    # (tagged with today's brief_date, per the app's actual behavior) but
    # its own start_time falls on a different date than `today`.
    start = datetime.combine(tomorrow, time(10, 0), tzinfo=TZ).isoformat()
    end = datetime.combine(tomorrow, time(11, 0), tzinfo=TZ).isoformat()
    _add_calendar_event(today.isoformat(), start, end)

    slots = availability.free_slots(tomorrow)

    # tomorrow's slots are computed purely from day bounds + recurring
    # commitments — the calendar row above must NOT be subtracted.
    assert slots == [(time(7, 0), time(22, 0))]


# --- calendar_checked / free_slots_for_week --------------------------------


def test_calendar_checked_true_only_for_today(isolated_db: None) -> None:
    today = _today()
    assert availability.calendar_checked(today) is True
    assert availability.calendar_checked(today + timedelta(days=1)) is False
    assert availability.calendar_checked(today - timedelta(days=1)) is False


def test_free_slots_for_week_returns_seven_dates(isolated_db: None) -> None:
    start = _today() + timedelta(days=10)

    week = availability.free_slots_for_week(start)

    assert set(week.keys()) == {start + timedelta(days=i) for i in range(7)}
    for slots in week.values():
        assert slots == [(time(7, 0), time(22, 0))]
