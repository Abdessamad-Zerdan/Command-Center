from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db
from command_center.assistant import chat, ingest
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


def test_standalone_assistant_page_no_longer_exists(client: TestClient) -> None:
    # Replaced by the floating widget embedded in dashboard.html — this
    # confirms the old full-page route was actually removed, not just
    # unlinked.
    response = client.get("/assistant")
    assert response.status_code == 404


def test_brief_page_includes_assistant_widget(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert "assistantWidget()" in response.text
    assert "/assistant/ask" in response.text


def test_history_page_does_not_include_assistant_widget(client: TestClient) -> None:
    from command_center import queries

    queries.create_manual_item("2020-01-01", "urgent", "An old task")

    response = client.get("/history/2020-01-01")

    assert response.status_code == 200
    assert "assistantWidget()" not in response.text


def test_ask_returns_grounded_answer(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chat,
        "answer",
        lambda question, history=None: {"answer": "Here you go.", "sources": ["Current Focus"]},
    )

    response = client.post("/assistant/ask", json={"question": "What am I working on?"})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["answer"] == "Here you go."
    assert data["sources"] == ["Current Focus"]


def test_ask_forwards_history_to_chat_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    def _fake(question, history=None):
        captured["question"] = question
        captured["history"] = history
        return {"answer": "ok", "sources": []}

    monkeypatch.setattr(chat, "answer", _fake)

    response = client.post(
        "/assistant/ask",
        json={
            "question": "next Friday",
            "history": [
                {"role": "user", "content": "create a task to renew my license"},
                {"role": "assistant", "content": "When is this due?"},
            ],
        },
    )

    assert response.status_code == 200
    assert captured["question"] == "next Friday"
    assert captured["history"] == [
        {"role": "user", "content": "create a task to renew my license"},
        {"role": "assistant", "content": "When is this due?"},
    ]


def test_ask_rejects_empty_question(client: TestClient) -> None:
    response = client.post("/assistant/ask", json={"question": "   "})
    assert response.status_code == 400


def test_ask_surfaces_provider_error_as_plain_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.triage import TriageProviderError

    def _raise(question, history=None):
        raise TriageProviderError("Groq request failed: authentication error.")

    monkeypatch.setattr(chat, "answer", _raise)

    response = client.post("/assistant/ask", json={"question": "hi"})

    assert response.status_code == 502
    assert "authentication error" in response.json()["error"]


def test_confirm_route_returns_result_from_confirm_action(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    def _fake_confirm_action(pending_action, confirmed):
        captured["pending_action"] = pending_action
        captured["confirmed"] = confirmed
        return {"answer": "Done — created 'X'.", "sources": []}

    monkeypatch.setattr(chat, "confirm_action", _fake_confirm_action)

    response = client.post(
        "/assistant/confirm",
        json={
            "pending_action": {
                "tool": "create_task",
                "args": {"title": "X"},
                "confirmation_text": "Create task 'X'?",
            },
            "confirmed": True,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["answer"] == "Done — created 'X'."
    assert captured["confirmed"] is True
    assert captured["pending_action"]["tool"] == "create_task"


def test_confirm_batch_route_forwards_tasks_and_confirmed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    def _fake_confirm_batch(tasks, confirmed):
        captured["tasks"] = tasks
        captured["confirmed"] = confirmed
        return {"answer": "Created 2 tasks: 'A', 'B'.", "sources": []}

    monkeypatch.setattr(chat, "confirm_batch", _fake_confirm_batch)

    response = client.post(
        "/assistant/confirm-batch",
        json={
            "tasks": [
                {"tool": "create_task", "args": {"title": "A"}, "confirmation_text": "Create task 'A'?", "checked": True},
                {"tool": "create_task", "args": {"title": "B"}, "confirmation_text": "Create task 'B'?", "checked": False},
            ],
            "confirmed": True,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["answer"] == "Created 2 tasks: 'A', 'B'."
    assert captured["confirmed"] is True
    assert len(captured["tasks"]) == 2
    assert captured["tasks"][0]["checked"] is True
    assert captured["tasks"][1]["checked"] is False


def test_brief_page_includes_batch_confirmation_widget_code(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert "resolveBatch" in response.text
    assert "/assistant/confirm-batch" in response.text


def test_rebuild_index_route_forces_rebuild(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(
        ingest, "rebuild_index", lambda force=False: calls.append(force) or True
    )

    response = client.post("/settings/assistant/rebuild-index", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert calls == [True]


def test_rebuild_index_route_degrades_instead_of_500ing_on_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A first-run model download can fail offline (same risk the startup
    # call already guards against in app.py's lifespan) — the manually
    # triggered Settings-page button must degrade the same way, not
    # surface a raw 500.
    def _raise(force=False):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(ingest, "rebuild_index", _raise)

    response = client.post("/settings/assistant/rebuild-index", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


def test_settings_page_shows_index_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ingest,
        "get_index_status",
        lambda: {"last_indexed_at": "2026-08-14T10:00:00+03:00", "chunk_count": 7},
    )

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Assistant index" in response.text
    assert "7 chunks" in response.text
