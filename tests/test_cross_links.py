"""Tasks linked across the app: a task's permanent address, its
competition link, competition task lists, and Pomodoro task rows that
can be deleted and lead back to their task."""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import attachments, auth, db, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.config import TZ
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", tmp_path / "attachments")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _competition(client: TestClient, title: str = "NASA Space Apps") -> int:
    response = client.post("/competitions", json={"title": title})
    assert response.status_code == 200, response.text
    return response.json()["id"]


# --- a task's home address ----------------------------------------------------


def test_open_redirects_todays_task_to_the_brief(client: TestClient) -> None:
    item_id = queries.create_manual_item(_today(), "action_items", "Draft pitch")

    response = client.get(f"/items/{item_id}/open", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/brief?open={item_id}"


def test_open_redirects_an_older_task_to_its_day(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-01-05", "action_items", "Old thing")

    response = client.get(f"/items/{item_id}/open", follow_redirects=False)

    assert response.headers["location"] == f"/history/2026-01-05?open={item_id}"


def test_open_unknown_task_is_404(client: TestClient) -> None:
    assert client.get("/items/999/open", follow_redirects=False).status_code == 404


def test_get_item_includes_link_names(client: TestClient) -> None:
    project_id = queries.create_registered_project("Command Center", "D:/cc")
    competition_id = _competition(client)
    item_id = queries.create_manual_item(_today(), "action_items", "Build demo", project_id=project_id)
    queries.set_item_competition(item_id, competition_id)

    body = client.get(f"/items/{item_id}").json()

    assert body["title"] == "Build demo"
    assert body["project_name"] == "Command Center"
    assert body["competition_title"] == "NASA Space Apps"
    assert body["attachment_count"] == 0


def test_get_unknown_item_is_404(client: TestClient) -> None:
    assert client.get("/items/999").status_code == 404


# --- linking a task to a competition -------------------------------------------


def test_link_and_unlink_a_competition(client: TestClient) -> None:
    competition_id = _competition(client)
    item_id = queries.create_manual_item(_today(), "action_items", "Register team")

    assert client.patch(f"/items/{item_id}/competition", json={"competition_id": competition_id}).status_code == 200
    assert queries.get_item(item_id)["competition_id"] == competition_id

    assert client.patch(f"/items/{item_id}/competition", json={"competition_id": None}).status_code == 200
    assert queries.get_item(item_id)["competition_id"] is None


def test_link_to_unknown_competition_is_refused(client: TestClient) -> None:
    item_id = queries.create_manual_item(_today(), "action_items", "Register team")

    response = client.patch(f"/items/{item_id}/competition", json={"competition_id": 404})

    assert response.status_code == 400
    assert queries.get_item(item_id)["competition_id"] is None


def test_link_unknown_item_is_404(client: TestClient) -> None:
    competition_id = _competition(client)
    assert client.patch("/items/999/competition", json={"competition_id": competition_id}).status_code == 404


def test_linked_card_shows_competition_chip_on_the_brief(client: TestClient) -> None:
    competition_id = _competition(client, "ETH Global")
    item_id = queries.create_manual_item(_today(), "action_items", "Find teammates")
    queries.set_item_competition(item_id, competition_id)

    html = client.get("/brief").text

    assert f'href="/competitions?open={competition_id}"' in html
    assert "ETH Global" in html
    assert f'data-item-id="{item_id}"' in html


def test_deleting_a_competition_unlinks_its_tasks(client: TestClient) -> None:
    competition_id = _competition(client)
    item_id = queries.create_manual_item(_today(), "action_items", "Register team")
    queries.set_item_competition(item_id, competition_id)

    assert client.delete(f"/competitions/{competition_id}").status_code == 200

    item = queries.get_item(item_id)
    assert item is not None  # the task itself stays
    assert item["competition_id"] is None


# --- a competition's tasks -------------------------------------------------------


def test_add_task_for_a_competition_lands_on_today_linked(client: TestClient) -> None:
    competition_id = _competition(client)

    response = client.post(f"/competitions/{competition_id}/tasks", json={"title": "  Submit idea  "})

    assert response.status_code == 200, response.text
    task = response.json()
    assert task["title"] == "Submit idea"
    assert task["status"] == "pending"
    assert task["brief_date"] == _today()
    assert task["url"] == f"/items/{task['id']}/open"
    assert queries.get_item(task["id"])["competition_id"] == competition_id


def test_add_task_needs_a_name(client: TestClient) -> None:
    competition_id = _competition(client)
    assert client.post(f"/competitions/{competition_id}/tasks", json={"title": "   "}).status_code == 400
    assert client.post(f"/competitions/{competition_id}/tasks", json={"title": "x" * 301}).status_code == 400


def test_add_task_to_unknown_competition_is_404(client: TestClient) -> None:
    assert client.post("/competitions/999/tasks", json={"title": "Anything"}).status_code == 404


def test_competition_tasks_list_open_ones_first(client: TestClient) -> None:
    competition_id = _competition(client)
    other_id = _competition(client, "Other")
    done_id = queries.create_manual_item(_today(), "action_items", "Done thing")
    queries.set_item_competition(done_id, competition_id)
    queries.set_item_status(done_id, "done")
    open_id = queries.create_manual_item("2026-01-05", "action_items", "Open thing")
    queries.set_item_competition(open_id, competition_id)
    elsewhere = queries.create_manual_item(_today(), "action_items", "Not this one")
    queries.set_item_competition(elsewhere, other_id)

    tasks = client.get(f"/competitions/{competition_id}/tasks").json()

    assert [t["id"] for t in tasks] == [open_id, done_id]
    assert queries.get_competition_open_task_counts() == {competition_id: 1, other_id: 1}


# --- Pomodoro task rows ------------------------------------------------------------


def test_delete_pomodoro_task_removes_all_its_sessions(client: TestClient) -> None:
    for _ in range(2):
        s = queries.start_pomodoro_session("Write docs", 25)
        queries.finish_pomodoro_session(s, elapsed_seconds=600, status="completed")
    keep = queries.start_pomodoro_session("Fix bug", 25)
    queries.finish_pomodoro_session(keep, elapsed_seconds=300, status="completed")

    response = client.delete("/pomodoro/tasks", params={"name": "Write docs"})

    assert response.status_code == 200
    assert response.json() == {"deleted": 2}
    assert [t["task_name"] for t in queries.get_time_by_task()] == ["Fix bug"]


def test_delete_unknown_pomodoro_task_is_404(client: TestClient) -> None:
    assert client.delete("/pomodoro/tasks", params={"name": "Nope"}).status_code == 404


def test_time_by_task_carries_the_task_it_came_from(client: TestClient) -> None:
    item_id = queries.create_manual_item(_today(), "action_items", "Build demo")
    s1 = queries.start_pomodoro_session("Build demo", 25, task_source_id=item_id)
    queries.finish_pomodoro_session(s1, elapsed_seconds=600, status="completed")
    s2 = queries.start_pomodoro_session("Freeform", 25)
    queries.finish_pomodoro_session(s2, elapsed_seconds=300, status="completed")

    by_name = {t["task_name"]: t["task_source_id"] for t in queries.get_time_by_task()}

    assert by_name == {"Build demo": item_id, "Freeform": None}
