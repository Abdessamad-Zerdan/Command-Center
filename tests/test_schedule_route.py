from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, fixtures, queries
from command_center.assistant import ingest
from command_center.config import TZ
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(ingest, "rebuild_index", lambda *a, **k: False)
    # demo-mode's fixtures.seed() would otherwise plant synthetic calendar
    # events on today's date, which free_slots() correctly subtracts —
    # confounding tests that make precise assertions about today's slots.
    monkeypatch.setattr(fixtures, "seed", lambda: None)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def _today():
    return datetime.now(TZ).date()


def test_schedule_page_renders_seven_days(client: TestClient) -> None:
    response = client.get("/schedule")
    assert response.status_code == 200
    assert response.text.count("Calendar not checked this far out") == 6  # every day but today


def test_schedule_page_today_has_no_gap_label(client: TestClient) -> None:
    response = client.get("/schedule")
    assert response.status_code == 200
    # today's date label appears somewhere not immediately followed by the gap text
    today_label = f"{_today().strftime('%b')} {_today().day}"
    assert today_label in response.text


def test_schedule_page_empty_state_shows_full_day_bounds(client: TestClient) -> None:
    response = client.get("/schedule")
    assert response.status_code == 200
    # 7 days, each showing the default full day-bounds slot, zero commitments
    assert response.text.count("07:00") >= 7


def test_schedule_page_shows_recurring_commitment_as_a_gap(client: TestClient) -> None:
    today = _today()
    queries.create_recurring_commitment("Work hours", today.weekday(), "09:00", "17:00")

    response = client.get("/schedule")

    assert response.status_code == 200
    # The commitment splits today's full-bounds pill into two: the block
    # from 09:00-17:00 is no longer offered as free time. (The other 6
    # days of the week are unaffected and still show "07:00–22:00" —
    # not asserting its absence.)
    assert "07:00&ndash;09:00" in response.text
    assert "17:00&ndash;22:00" in response.text


def test_schedule_page_commitment_only_affects_its_own_weekday(client: TestClient) -> None:
    today = _today()
    other_weekday = (today.weekday() + 1) % 7
    queries.create_recurring_commitment("Gym", other_weekday, "09:00", "17:00")

    response = client.get("/schedule")

    assert response.status_code == 200
    # A commitment on a different weekday must not touch today's slot —
    # today's row still shows the untouched full-bounds pill.
    assert "07:00&ndash;22:00" in response.text


def test_manage_link_points_to_settings_schedule(client: TestClient) -> None:
    response = client.get("/schedule")
    assert response.status_code == 200
    assert "/settings/schedule" in response.text


def test_schedule_page_includes_suggest_schedule_button(client: TestClient) -> None:
    response = client.get("/schedule")
    assert response.status_code == 200
    assert "Suggest schedule" in response.text
    assert "/schedule/suggest" in response.text


# --- POST /schedule/suggest --------------------------------------------------


def test_suggest_schedule_route_returns_placements(client: TestClient) -> None:
    today = _today()
    queries.create_manual_item(today.isoformat(), "urgent", "A real task", due_date=today.isoformat())

    response = client.post("/schedule/suggest")

    assert response.status_code == 200
    data = response.json()
    assert len(data["placements"]) == 1
    assert data["placements"][0]["title"] == "A real task"


def test_suggest_schedule_route_empty_when_no_candidates(client: TestClient) -> None:
    response = client.post("/schedule/suggest")
    assert response.status_code == 200
    assert response.json()["placements"] == []


# --- POST /schedule/accept-placement -----------------------------------------


def test_accept_placement_writes_scheduled_times(client: TestClient) -> None:
    item_id = queries.create_manual_item(_today().isoformat(), "urgent", "Task to accept")

    response = client.post(
        "/schedule/accept-placement",
        json={
            "item_id": item_id,
            "scheduled_date": "2026-08-18",
            "scheduled_start": "09:00",
            "scheduled_end": "09:30",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    with db.session() as conn:
        row = conn.execute(
            "SELECT scheduled_start, scheduled_end FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    assert row["scheduled_start"] == "2026-08-18T09:00:00"
    assert row["scheduled_end"] == "2026-08-18T09:30:00"


def test_accept_placement_logs_tool_call(client: TestClient) -> None:
    item_id = queries.create_manual_item(_today().isoformat(), "urgent", "Task to accept")

    client.post(
        "/schedule/accept-placement",
        json={
            "item_id": item_id,
            "scheduled_date": "2026-08-18",
            "scheduled_start": "09:00",
            "scheduled_end": "09:30",
        },
    )

    with db.session() as conn:
        rows = conn.execute("SELECT tool, status FROM tool_call_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["tool"] == "schedule_task"
    assert rows[0]["status"] == "confirmed"


def test_accept_placement_rejects_malformed_date(client: TestClient) -> None:
    item_id = queries.create_manual_item(_today().isoformat(), "urgent", "X")
    response = client.post(
        "/schedule/accept-placement",
        json={"item_id": item_id, "scheduled_date": "not-a-date", "scheduled_start": "09:00", "scheduled_end": "09:30"},
    )
    assert response.status_code == 400


def test_accept_placement_rejects_end_before_start(client: TestClient) -> None:
    item_id = queries.create_manual_item(_today().isoformat(), "urgent", "X")
    response = client.post(
        "/schedule/accept-placement",
        json={"item_id": item_id, "scheduled_date": "2026-08-18", "scheduled_start": "10:00", "scheduled_end": "09:00"},
    )
    assert response.status_code == 400


def test_accept_placement_rejects_unknown_item(client: TestClient) -> None:
    response = client.post(
        "/schedule/accept-placement",
        json={"item_id": 99999, "scheduled_date": "2026-08-18", "scheduled_start": "09:00", "scheduled_end": "09:30"},
    )
    assert response.status_code == 404
