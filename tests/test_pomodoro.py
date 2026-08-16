from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_create_and_list_pomodoro_history(isolated_db: None) -> None:
    queries.create_pomodoro_session(
        task_name="Write docs",
        planned_minutes=25,
        elapsed_seconds=1500,
        started_at="2026-08-13T10:00:00+03:00",
        ended_at="2026-08-13T10:25:00+03:00",
        status="completed",
    )
    queries.create_pomodoro_session(
        task_name="Fix bug",
        planned_minutes=25,
        elapsed_seconds=300,
        started_at="2026-08-13T11:00:00+03:00",
        ended_at="2026-08-13T11:05:00+03:00",
        status="stopped_early",
    )

    history = queries.list_pomodoro_history()
    assert len(history) == 2
    # Newest first
    assert history[0]["task_name"] == "Fix bug"
    assert history[0]["status"] == "stopped_early"
    assert history[1]["task_name"] == "Write docs"


def test_get_time_by_task_aggregates_across_sessions(isolated_db: None) -> None:
    queries.create_pomodoro_session(
        task_name="Write docs",
        planned_minutes=25,
        elapsed_seconds=1500,
        started_at="2026-08-13T10:00:00+03:00",
        ended_at="2026-08-13T10:25:00+03:00",
        status="completed",
    )
    queries.create_pomodoro_session(
        task_name="Write docs",
        planned_minutes=25,
        elapsed_seconds=900,
        started_at="2026-08-13T14:00:00+03:00",
        ended_at="2026-08-13T14:15:00+03:00",
        status="stopped_early",
    )
    queries.create_pomodoro_session(
        task_name="Fix bug",
        planned_minutes=5,
        elapsed_seconds=300,
        started_at="2026-08-13T11:00:00+03:00",
        ended_at="2026-08-13T11:05:00+03:00",
        status="completed",
    )

    totals = queries.get_time_by_task()
    by_name = {row["task_name"]: row["total_seconds"] for row in totals}
    assert by_name["Write docs"] == 2400
    assert by_name["Fix bug"] == 300
    # Sorted descending by total time
    assert totals[0]["task_name"] == "Write docs"


def test_history_limit_respected(isolated_db: None) -> None:
    for i in range(5):
        queries.create_pomodoro_session(
            task_name=f"Task {i}",
            planned_minutes=25,
            elapsed_seconds=1500,
            started_at="2026-08-13T10:00:00+03:00",
            ended_at="2026-08-13T10:25:00+03:00",
            status="completed",
        )
    assert len(queries.list_pomodoro_history(limit=3)) == 3


def test_delete_pomodoro_session_returns_deleted_row(isolated_db: None) -> None:
    session_id = queries.create_pomodoro_session(
        task_name="Write docs",
        planned_minutes=25,
        elapsed_seconds=1500,
        started_at="2026-08-13T10:00:00+03:00",
        ended_at="2026-08-13T10:25:00+03:00",
        status="completed",
    )
    deleted = queries.delete_pomodoro_session(session_id)
    assert deleted is not None
    assert deleted["task_name"] == "Write docs"
    assert deleted["elapsed_seconds"] == 1500
    assert queries.list_pomodoro_history() == []


def test_delete_pomodoro_session_returns_none_for_missing(isolated_db: None) -> None:
    assert queries.delete_pomodoro_session(9999) is None


def test_rename_pomodoro_task_updates_all_matching_sessions(isolated_db: None) -> None:
    queries.create_pomodoro_session(
        task_name="Old name",
        planned_minutes=25,
        elapsed_seconds=1500,
        started_at="2026-08-13T10:00:00+03:00",
        ended_at="2026-08-13T10:25:00+03:00",
        status="completed",
    )
    queries.create_pomodoro_session(
        task_name="Old name",
        planned_minutes=25,
        elapsed_seconds=600,
        started_at="2026-08-13T12:00:00+03:00",
        ended_at="2026-08-13T12:10:00+03:00",
        status="stopped_early",
    )

    affected = queries.rename_pomodoro_task("Old name", "New name")
    assert affected == 2

    history = queries.list_pomodoro_history()
    assert all(row["task_name"] == "New name" for row in history)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def test_create_pomodoro_session_endpoint(client: TestClient) -> None:
    response = client.post(
        "/pomodoro/sessions",
        json={
            "task_name": "Ship feature",
            "planned_minutes": 25,
            "elapsed_seconds": 1500,
            "status": "completed",
        },
    )
    assert response.status_code == 200
    session_id = response.json()["id"]
    assert isinstance(session_id, int)

    history = queries.list_pomodoro_history()
    assert history[0]["task_name"] == "Ship feature"
    assert history[0]["id"] == session_id


def test_delete_pomodoro_session_endpoint(client: TestClient) -> None:
    create = client.post(
        "/pomodoro/sessions",
        json={
            "task_name": "Delete me",
            "planned_minutes": 25,
            "elapsed_seconds": 1500,
            "status": "completed",
        },
    )
    session_id = create.json()["id"]

    response = client.delete(f"/pomodoro/sessions/{session_id}")
    assert response.status_code == 200
    assert response.json()["task_name"] == "Delete me"
    assert queries.list_pomodoro_history() == []


def test_delete_pomodoro_session_endpoint_404_for_missing(client: TestClient) -> None:
    response = client.delete("/pomodoro/sessions/99999")
    assert response.status_code == 404


def test_rename_pomodoro_task_endpoint(client: TestClient) -> None:
    client.post(
        "/pomodoro/sessions",
        json={
            "task_name": "Before",
            "planned_minutes": 25,
            "elapsed_seconds": 1500,
            "status": "completed",
        },
    )
    response = client.patch(
        "/pomodoro/tasks", json={"old_name": "Before", "new_name": "After"}
    )
    assert response.status_code == 200
    assert response.json()["renamed"] == 1
    assert queries.list_pomodoro_history()[0]["task_name"] == "After"


def test_home_page_includes_pomodoro_widget(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "pomodoroTimer" in response.text
    assert "What are you working on?" in response.text
    # The old avatar/bio content should be gone
    assert "I build AI tools" not in response.text
