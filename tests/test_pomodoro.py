from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.config import TZ
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


# --- start / heartbeat / finish lifecycle -----------------------------------


def test_start_pomodoro_session_creates_an_in_progress_row(isolated_db: None) -> None:
    session_id = queries.start_pomodoro_session("Write docs", 25)

    history = queries.list_pomodoro_history()
    assert history == []  # in_progress rows are excluded from the history list

    with db.session() as conn:
        row = dict(conn.execute("SELECT * FROM pomodoro_sessions WHERE id = ?", (session_id,)).fetchone())
    assert row["status"] == "in_progress"
    assert row["elapsed_seconds"] == 0
    assert row["task_name"] == "Write docs"
    assert row["task_source_id"] is None


def test_start_pomodoro_session_links_a_task_source_id(isolated_db: None) -> None:
    session_id = queries.start_pomodoro_session("Ship feature", 25, task_source_id=42)
    with db.session() as conn:
        row = dict(conn.execute("SELECT task_source_id FROM pomodoro_sessions WHERE id = ?", (session_id,)).fetchone())
    assert row["task_source_id"] == 42


def test_heartbeat_updates_elapsed_seconds_on_an_in_progress_session(isolated_db: None) -> None:
    session_id = queries.start_pomodoro_session("Write docs", 25)

    updated = queries.update_pomodoro_heartbeat(session_id, elapsed_seconds=1080)  # 18 minutes

    assert updated is True
    with db.session() as conn:
        row = dict(conn.execute("SELECT elapsed_seconds FROM pomodoro_sessions WHERE id = ?", (session_id,)).fetchone())
    assert row["elapsed_seconds"] == 1080


def test_heartbeat_records_paused_at_and_resumed_at(isolated_db: None) -> None:
    session_id = queries.start_pomodoro_session("Write docs", 25)
    queries.update_pomodoro_heartbeat(session_id, elapsed_seconds=720, paused_at="2026-08-19T12:00:00+03:00")
    queries.update_pomodoro_heartbeat(session_id, elapsed_seconds=720, resumed_at="2026-08-19T14:00:00+03:00")

    with db.session() as conn:
        row = dict(conn.execute("SELECT paused_at, resumed_at FROM pomodoro_sessions WHERE id = ?", (session_id,)).fetchone())
    assert row["paused_at"] == "2026-08-19T12:00:00+03:00"
    assert row["resumed_at"] == "2026-08-19T14:00:00+03:00"


def test_heartbeat_does_nothing_to_an_already_finished_session(isolated_db: None) -> None:
    session_id = queries.start_pomodoro_session("Write docs", 25)
    queries.finish_pomodoro_session(session_id, elapsed_seconds=1500, status="completed")

    updated = queries.update_pomodoro_heartbeat(session_id, elapsed_seconds=99999)

    assert updated is False
    with db.session() as conn:
        row = dict(conn.execute("SELECT elapsed_seconds FROM pomodoro_sessions WHERE id = ?", (session_id,)).fetchone())
    assert row["elapsed_seconds"] == 1500  # untouched by the stale heartbeat


def test_heartbeat_returns_false_for_unknown_session(isolated_db: None) -> None:
    assert queries.update_pomodoro_heartbeat(99999, elapsed_seconds=100) is False


def test_finish_pomodoro_session_sets_final_state(isolated_db: None) -> None:
    session_id = queries.start_pomodoro_session("Write docs", 25)

    row = queries.finish_pomodoro_session(session_id, elapsed_seconds=1080, status="stopped_early")

    assert row["status"] == "stopped_early"
    assert row["elapsed_seconds"] == 1080
    assert row["ended_at"] is not None
    history = queries.list_pomodoro_history()
    assert len(history) == 1
    assert history[0]["id"] == session_id


def test_finish_pomodoro_session_records_break_duration(isolated_db: None) -> None:
    session_id = queries.start_pomodoro_session("Write docs", 25)
    queries.finish_pomodoro_session(session_id, elapsed_seconds=1500, status="completed")

    row = queries.finish_pomodoro_session(
        session_id, elapsed_seconds=1500, status="completed", break_duration_sec=300
    )

    assert row["break_duration_sec"] == 300


def test_finish_pomodoro_session_returns_none_for_unknown(isolated_db: None) -> None:
    assert queries.finish_pomodoro_session(99999, elapsed_seconds=100, status="completed") is None


# --- validation scenario: partial session (18 of 25 minutes) ----------------


def test_partial_session_logs_actual_worked_time_not_zero_or_full(isolated_db: None) -> None:
    # Simulates: start a 25m session, heartbeats bring it to 18m worked,
    # then the "browser closes" — the last heartbeat is what's on disk,
    # not 0 (never logged) and not 25m (the old force-full-duration bug).
    session_id = queries.start_pomodoro_session("Deep work", 25)
    queries.update_pomodoro_heartbeat(session_id, elapsed_seconds=300)
    queries.update_pomodoro_heartbeat(session_id, elapsed_seconds=600)
    queries.update_pomodoro_heartbeat(session_id, elapsed_seconds=1080)  # 18 minutes, last save before "close"

    with db.session() as conn:
        row = dict(conn.execute("SELECT elapsed_seconds, status FROM pomodoro_sessions WHERE id = ?", (session_id,)).fetchone())
    assert row["elapsed_seconds"] == 1080
    assert row["status"] == "in_progress"  # never explicitly finished, but the real time is already safe


# --- time aggregation ---------------------------------------------------------


def test_get_time_by_task_aggregates_across_sessions(isolated_db: None) -> None:
    s1 = queries.start_pomodoro_session("Write docs", 25)
    queries.finish_pomodoro_session(s1, elapsed_seconds=1500, status="completed")
    s2 = queries.start_pomodoro_session("Write docs", 25)
    queries.finish_pomodoro_session(s2, elapsed_seconds=900, status="stopped_early")
    s3 = queries.start_pomodoro_session("Fix bug", 5)
    queries.finish_pomodoro_session(s3, elapsed_seconds=300, status="completed")

    totals = queries.get_time_by_task()
    by_name = {row["task_name"]: row["total_seconds"] for row in totals}
    assert by_name["Write docs"] == 2400
    assert by_name["Fix bug"] == 300
    assert totals[0]["task_name"] == "Write docs"  # sorted descending


def test_get_time_by_task_includes_in_progress_sessions(isolated_db: None) -> None:
    # A still-running session's logged time counts toward the total
    # immediately via heartbeats, not just once it's finished.
    session_id = queries.start_pomodoro_session("Live task", 25)
    queries.update_pomodoro_heartbeat(session_id, elapsed_seconds=600)

    totals = queries.get_time_by_task()
    assert totals[0]["task_name"] == "Live task"
    assert totals[0]["total_seconds"] == 600


def test_get_time_logged_by_source_ids_groups_by_item(isolated_db: None) -> None:
    s1 = queries.start_pomodoro_session("Task A", 25, task_source_id=101)
    queries.finish_pomodoro_session(s1, elapsed_seconds=1500, status="completed")
    s2 = queries.start_pomodoro_session("Task A", 25, task_source_id=101)
    queries.finish_pomodoro_session(s2, elapsed_seconds=600, status="stopped_early")
    s3 = queries.start_pomodoro_session("Task B", 25, task_source_id=202)
    queries.finish_pomodoro_session(s3, elapsed_seconds=300, status="completed")

    totals = queries.get_time_logged_by_source_ids([101, 202])
    assert totals[101] == 2100
    assert totals[202] == 300


def test_get_time_logged_by_source_ids_omits_items_with_no_sessions(isolated_db: None) -> None:
    assert queries.get_time_logged_by_source_ids([999]) == {}


def test_get_time_logged_by_source_ids_empty_list_returns_empty_dict(isolated_db: None) -> None:
    assert queries.get_time_logged_by_source_ids([]) == {}


def test_get_time_logged_by_source_ids_ignores_anonymous_sessions(isolated_db: None) -> None:
    session_id = queries.start_pomodoro_session("Anonymous task", 25)  # no task_source_id
    queries.finish_pomodoro_session(session_id, elapsed_seconds=1500, status="completed")

    assert queries.get_time_logged_by_source_ids([1, 2, 3]) == {}


# --- history / delete / rename (existing behavior, still correct) -----------


def test_history_limit_respected(isolated_db: None) -> None:
    for i in range(5):
        session_id = queries.start_pomodoro_session(f"Task {i}", 25)
        queries.finish_pomodoro_session(session_id, elapsed_seconds=1500, status="completed")
    assert len(queries.list_pomodoro_history(limit=3)) == 3


def test_delete_pomodoro_session_returns_deleted_row(isolated_db: None) -> None:
    session_id = queries.start_pomodoro_session("Write docs", 25)
    queries.finish_pomodoro_session(session_id, elapsed_seconds=1500, status="completed")

    deleted = queries.delete_pomodoro_session(session_id)
    assert deleted is not None
    assert deleted["task_name"] == "Write docs"
    assert deleted["elapsed_seconds"] == 1500
    assert queries.list_pomodoro_history() == []


def test_delete_pomodoro_session_returns_none_for_missing(isolated_db: None) -> None:
    assert queries.delete_pomodoro_session(9999) is None


def test_rename_pomodoro_task_updates_all_matching_sessions(isolated_db: None) -> None:
    s1 = queries.start_pomodoro_session("Old name", 25)
    queries.finish_pomodoro_session(s1, elapsed_seconds=1500, status="completed")
    s2 = queries.start_pomodoro_session("Old name", 25)
    queries.finish_pomodoro_session(s2, elapsed_seconds=600, status="stopped_early")

    affected = queries.rename_pomodoro_task("Old name", "New name")
    assert affected == 2

    history = queries.list_pomodoro_history()
    assert all(row["task_name"] == "New name" for row in history)


# --- routes -------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def test_start_session_endpoint(client: TestClient) -> None:
    response = client.post(
        "/pomodoro/sessions", json={"task_name": "Ship feature", "planned_minutes": 25}
    )
    assert response.status_code == 200
    session_id = response.json()["id"]
    assert isinstance(session_id, int)

    with db.session() as conn:
        row = dict(conn.execute("SELECT status FROM pomodoro_sessions WHERE id = ?", (session_id,)).fetchone())
    assert row["status"] == "in_progress"


def test_start_session_endpoint_accepts_task_source_id(client: TestClient) -> None:
    response = client.post(
        "/pomodoro/sessions",
        json={"task_name": "Linked task", "planned_minutes": 25, "task_source_id": 7},
    )
    with db.session() as conn:
        row = dict(conn.execute(
            "SELECT task_source_id FROM pomodoro_sessions WHERE id = ?", (response.json()["id"],)
        ).fetchone())
    assert row["task_source_id"] == 7


def test_start_session_endpoint_rejects_blank_task_name(client: TestClient) -> None:
    response = client.post("/pomodoro/sessions", json={"task_name": "   ", "planned_minutes": 25})
    assert response.status_code == 400


def test_start_session_endpoint_rejects_non_positive_duration(client: TestClient) -> None:
    response = client.post("/pomodoro/sessions", json={"task_name": "X", "planned_minutes": 0})
    assert response.status_code == 400


def test_start_session_endpoint_accepts_a_custom_duration(client: TestClient) -> None:
    # Free-form durations (not just 25/15/5) must work end to end.
    response = client.post("/pomodoro/sessions", json={"task_name": "X", "planned_minutes": 18})
    assert response.status_code == 200
    with db.session() as conn:
        row = dict(conn.execute(
            "SELECT planned_minutes FROM pomodoro_sessions WHERE id = ?", (response.json()["id"],)
        ).fetchone())
    assert row["planned_minutes"] == 18


def test_heartbeat_endpoint(client: TestClient) -> None:
    start = client.post("/pomodoro/sessions", json={"task_name": "X", "planned_minutes": 25})
    session_id = start.json()["id"]

    response = client.patch(f"/pomodoro/sessions/{session_id}/heartbeat", json={"elapsed_seconds": 1080})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    with db.session() as conn:
        row = dict(conn.execute("SELECT elapsed_seconds FROM pomodoro_sessions WHERE id = ?", (session_id,)).fetchone())
    assert row["elapsed_seconds"] == 1080


def test_heartbeat_endpoint_404s_for_unknown_session(client: TestClient) -> None:
    response = client.patch("/pomodoro/sessions/99999/heartbeat", json={"elapsed_seconds": 100})
    assert response.status_code == 404


def test_finish_endpoint(client: TestClient) -> None:
    start = client.post("/pomodoro/sessions", json={"task_name": "X", "planned_minutes": 25})
    session_id = start.json()["id"]

    response = client.post(
        f"/pomodoro/sessions/{session_id}/finish",
        json={"elapsed_seconds": 1500, "status": "completed"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["elapsed_seconds"] == 1500


def test_finish_endpoint_404s_for_unknown_session(client: TestClient) -> None:
    response = client.post(
        "/pomodoro/sessions/99999/finish", json={"elapsed_seconds": 100, "status": "completed"}
    )
    assert response.status_code == 404


def test_delete_pomodoro_session_endpoint(client: TestClient) -> None:
    start = client.post("/pomodoro/sessions", json={"task_name": "Delete me", "planned_minutes": 25})
    session_id = start.json()["id"]
    client.post(f"/pomodoro/sessions/{session_id}/finish", json={"elapsed_seconds": 1500, "status": "completed"})

    response = client.delete(f"/pomodoro/sessions/{session_id}")
    assert response.status_code == 200
    assert response.json()["task_name"] == "Delete me"
    assert queries.list_pomodoro_history() == []


def test_delete_pomodoro_session_endpoint_404_for_missing(client: TestClient) -> None:
    response = client.delete("/pomodoro/sessions/99999")
    assert response.status_code == 404


def test_rename_pomodoro_task_endpoint(client: TestClient) -> None:
    start = client.post("/pomodoro/sessions", json={"task_name": "Before", "planned_minutes": 25})
    client.post(f"/pomodoro/sessions/{start.json()['id']}/finish", json={"elapsed_seconds": 1500, "status": "completed"})

    response = client.patch("/pomodoro/tasks", json={"old_name": "Before", "new_name": "After"})
    assert response.status_code == 200
    assert response.json()["renamed"] == 1
    assert queries.list_pomodoro_history()[0]["task_name"] == "After"


def test_home_page_includes_pomodoro_widget(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "pomodoroTimer" in response.text
    assert "What are you working on?" in response.text
    assert "I build AI tools" not in response.text


# --- task-linked time-logged badge on item cards -----------------------------


def test_brief_shows_time_logged_badge_for_a_linked_task(client: TestClient) -> None:
    today = datetime.now(TZ).date().isoformat()
    item_id = queries.create_manual_item(today, "action_items", "Deep work task")
    session_id = queries.start_pomodoro_session("Deep work task", 25, task_source_id=item_id)
    queries.finish_pomodoro_session(session_id, elapsed_seconds=5400, status="completed")  # 1h 30m

    response = client.get("/brief")

    assert "Time logged: 1h 30m" in response.text


def test_brief_omits_time_logged_badge_when_nothing_logged(client: TestClient) -> None:
    response = client.get("/brief")
    assert "Time logged:" not in response.text


def test_item_card_wires_up_start_pomodoro_link(client: TestClient) -> None:
    response = client.get("/brief")
    assert "pomodoro_task_id=" in response.text
    assert "Start Pomodoro" in response.text
