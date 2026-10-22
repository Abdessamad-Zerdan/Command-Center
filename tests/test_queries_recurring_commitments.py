from pathlib import Path

import pytest

from command_center import db, queries


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


# --- recurring_commitments CRUD ------------------------------------------


def test_create_and_list_recurring_commitment(isolated_db: None) -> None:
    commitment_id = queries.create_recurring_commitment("Work hours", 0, "09:00", "17:00")

    rows = queries.list_recurring_commitments()

    assert len(rows) == 1
    assert rows[0]["id"] == commitment_id
    assert rows[0]["title"] == "Work hours"
    assert rows[0]["day_of_week"] == 0
    assert rows[0]["start_time"] == "09:00"
    assert rows[0]["end_time"] == "17:00"
    assert rows[0]["active"] == 1


def test_list_recurring_commitments_filters_by_day_of_week(isolated_db: None) -> None:
    queries.create_recurring_commitment("Gym", 1, "18:00", "19:00")
    queries.create_recurring_commitment("Sync", 3, "10:00", "10:30")

    monday = queries.list_recurring_commitments(day_of_week=1)
    wednesday = queries.list_recurring_commitments(day_of_week=3)
    friday = queries.list_recurring_commitments(day_of_week=4)

    assert [r["title"] for r in monday] == ["Gym"]
    assert [r["title"] for r in wednesday] == ["Sync"]
    assert friday == []


def test_list_recurring_commitments_active_only(isolated_db: None) -> None:
    active_id = queries.create_recurring_commitment("Active thing", 2, "09:00", "10:00")
    inactive_id = queries.create_recurring_commitment("Inactive thing", 2, "11:00", "12:00")
    queries.update_recurring_commitment(inactive_id, active=False)

    all_rows = queries.list_recurring_commitments(day_of_week=2, active_only=False)
    active_rows = queries.list_recurring_commitments(day_of_week=2, active_only=True)

    assert {r["id"] for r in all_rows} == {active_id, inactive_id}
    assert {r["id"] for r in active_rows} == {active_id}


def test_update_recurring_commitment_partial_update_leaves_other_fields(isolated_db: None) -> None:
    commitment_id = queries.create_recurring_commitment("Work hours", 0, "09:00", "17:00")

    updated = queries.update_recurring_commitment(commitment_id, end_time="18:00")

    assert updated is True
    row = queries.list_recurring_commitments()[0]
    assert row["start_time"] == "09:00"
    assert row["end_time"] == "18:00"
    assert row["title"] == "Work hours"


def test_update_recurring_commitment_unknown_id_returns_false(isolated_db: None) -> None:
    assert queries.update_recurring_commitment(99999, title="X") is False


def test_update_recurring_commitment_no_fields_returns_false(isolated_db: None) -> None:
    commitment_id = queries.create_recurring_commitment("Work hours", 0, "09:00", "17:00")
    assert queries.update_recurring_commitment(commitment_id) is False


def test_delete_recurring_commitment(isolated_db: None) -> None:
    commitment_id = queries.create_recurring_commitment("Work hours", 0, "09:00", "17:00")

    deleted = queries.delete_recurring_commitment(commitment_id)

    assert deleted is True
    assert queries.list_recurring_commitments() == []


def test_delete_recurring_commitment_unknown_id_returns_false(isolated_db: None) -> None:
    assert queries.delete_recurring_commitment(99999) is False


# --- app_settings / day bounds ---------------------------------------------


def test_get_day_bounds_returns_defaults_after_seed(isolated_db: None) -> None:
    queries.seed_app_settings()
    assert queries.get_day_bounds() == ("07:00", "22:00")


def test_get_day_bounds_returns_defaults_even_without_seeding(isolated_db: None) -> None:
    # get_day_bounds must never crash just because seed_app_settings()
    # hasn't run yet (e.g. a test or script that only calls queries.py
    # directly) — falls back to the same defaults.
    assert queries.get_day_bounds() == ("07:00", "22:00")


def test_set_day_bounds_persists(isolated_db: None) -> None:
    queries.seed_app_settings()
    queries.set_day_bounds("06:00", "23:00")
    assert queries.get_day_bounds() == ("06:00", "23:00")


def test_seed_app_settings_does_not_overwrite_existing_values(isolated_db: None) -> None:
    queries.seed_app_settings()
    queries.set_day_bounds("06:00", "23:00")

    queries.seed_app_settings()  # re-seed, e.g. on next app startup

    assert queries.get_day_bounds() == ("06:00", "23:00")
