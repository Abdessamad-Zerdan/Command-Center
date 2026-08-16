from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, queries
from command_center.assistant import ingest
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def test_settings_schedule_page_renders_empty_state(client: TestClient) -> None:
    response = client.get("/settings/schedule")
    assert response.status_code == 200
    assert "No recurring commitments yet" in response.text
    assert "07:00" in response.text
    assert "22:00" in response.text


def test_settings_schedule_page_lists_commitments(client: TestClient) -> None:
    queries.create_recurring_commitment("Work hours", 0, "09:00", "17:00")
    response = client.get("/settings/schedule")
    assert response.status_code == 200
    assert "Work hours" in response.text
    assert "Monday" in response.text


def test_settings_schedule_controls_revert_on_a_failed_save(client: TestClient) -> None:
    queries.create_recurring_commitment("Work hours", 0, "09:00", "17:00")
    response = client.get("/settings/schedule")
    assert response.status_code == 200
    # Day bounds
    assert "this.start = this.savedStart" in response.text
    # Commitment active toggle
    assert "this.active = !value" in response.text


def test_create_commitment(client: TestClient) -> None:
    response = client.post(
        "/settings/schedule/commitments",
        json={"title": "Gym", "day_of_week": 1, "start_time": "18:00", "end_time": "19:00"},
    )
    assert response.status_code == 200
    commitment_id = response.json()["id"]
    rows = queries.list_recurring_commitments()
    assert rows[0]["id"] == commitment_id
    assert rows[0]["title"] == "Gym"


def test_create_commitment_rejects_empty_title(client: TestClient) -> None:
    response = client.post(
        "/settings/schedule/commitments",
        json={"title": "  ", "day_of_week": 1, "start_time": "18:00", "end_time": "19:00"},
    )
    assert response.status_code == 400


def test_create_commitment_rejects_day_of_week_out_of_range(client: TestClient) -> None:
    response = client.post(
        "/settings/schedule/commitments",
        json={"title": "Gym", "day_of_week": 7, "start_time": "18:00", "end_time": "19:00"},
    )
    assert response.status_code == 400


def test_create_commitment_rejects_end_before_start(client: TestClient) -> None:
    response = client.post(
        "/settings/schedule/commitments",
        json={"title": "Gym", "day_of_week": 1, "start_time": "19:00", "end_time": "18:00"},
    )
    assert response.status_code == 400


def test_patch_commitment_partial_update(client: TestClient) -> None:
    commitment_id = queries.create_recurring_commitment("Gym", 1, "18:00", "19:00")

    response = client.patch(f"/settings/schedule/commitments/{commitment_id}", json={"active": False})

    assert response.status_code == 200
    row = queries.list_recurring_commitments()[0]
    assert row["active"] == 0
    assert row["title"] == "Gym"


def test_patch_commitment_unknown_id_404s(client: TestClient) -> None:
    response = client.patch("/settings/schedule/commitments/99999", json={"active": False})
    assert response.status_code == 404


def test_delete_commitment(client: TestClient) -> None:
    commitment_id = queries.create_recurring_commitment("Gym", 1, "18:00", "19:00")

    response = client.delete(f"/settings/schedule/commitments/{commitment_id}")

    assert response.status_code == 200
    assert queries.list_recurring_commitments() == []


def test_delete_commitment_unknown_id_404s(client: TestClient) -> None:
    response = client.delete("/settings/schedule/commitments/99999")
    assert response.status_code == 404


def test_update_day_bounds(client: TestClient) -> None:
    response = client.patch(
        "/settings/schedule/day-bounds", json={"day_bounds_start": "06:00", "day_bounds_end": "23:00"}
    )
    assert response.status_code == 200
    assert queries.get_day_bounds() == ("06:00", "23:00")


def test_update_day_bounds_rejects_invalid_format(client: TestClient) -> None:
    response = client.patch(
        "/settings/schedule/day-bounds", json={"day_bounds_start": "not-a-time", "day_bounds_end": "23:00"}
    )
    assert response.status_code == 400


def test_update_day_bounds_rejects_inverted_range(client: TestClient) -> None:
    response = client.patch(
        "/settings/schedule/day-bounds", json={"day_bounds_start": "23:00", "day_bounds_end": "06:00"}
    )
    assert response.status_code == 400
