"""The brief's priority board (partials/lanes.html, brief_view cookie):
open work in High / Medium / Low columns, the user's own priority
changes, and an honest note when the model's sort failed."""

import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.config import TZ
from command_center.setup_wizard import status as setup_status

EVIL = "http://evil.example.com"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _column(html: str, key: str) -> str:
    """The HTML of one board column, up to the next column."""
    start = html.index(f'data-column="{key}"')
    nxt = html.find('data-column="', start + 1)
    return html[start: nxt if nxt != -1 else len(html)]


def _task(title: str, lane: str = "urgent", priority: int | None = None) -> int:
    item_id = queries.create_manual_item(_today(), lane, title)
    if priority is not None:
        queries.save_model_priorities([(item_id, priority, f"Because {title}.")])
    return item_id


def test_priority_board_is_the_default_view(client: TestClient) -> None:
    _task("Exam prep", lane="tasks_due", priority=1)
    _task("Reply to advisor", lane="action_items", priority=2)
    _task("Watch AI course", lane="urgent", priority=3)

    html = client.get("/brief").text

    assert "Exam prep" in _column(html, "priority-1")
    assert "Reply to advisor" in _column(html, "priority-2")
    assert "Watch AI course" in _column(html, "priority-3")
    # Meeting prep and Reading keep their own columns; work lanes don't.
    assert 'data-column="meeting_prep"' in html and 'data-column="reading"' in html
    assert 'data-column="urgent"' not in html and 'data-column="action_items"' not in html


def test_board_cards_show_their_section_and_the_models_reason(client: TestClient) -> None:
    _task("Exam prep", lane="tasks_due", priority=1)

    high = _column(client.get("/brief").text, "priority-1")

    assert "Because Exam prep." in high
    assert "High:" in high
    assert 'title="Section"' in high


def test_every_card_appears_once_so_open_links_still_work(client: TestClient) -> None:
    item_id = _task("Only once", priority=2)
    html = client.get("/brief").text
    assert len(re.findall(rf'data-item-id="{item_id}"', html)) == 1


def test_sections_view_is_one_cookie_away(client: TestClient) -> None:
    _task("Exam prep", lane="tasks_due", priority=1)
    client.cookies.set("brief_view", "sections")

    html = client.get("/brief").text

    assert 'data-column="tasks_due"' in html and "Exam prep" in _column(html, "tasks_due")
    assert 'data-column="priority-1"' not in html


def test_the_view_toggle_is_wired_to_alpine(client: TestClient) -> None:
    # Regression: the toggle's @click sat outside any x-data, so Alpine
    # never bound it and clicking "By section" did nothing.
    html = client.get("/brief").text
    toggle = html[html.rindex("<div", 0, html.index('aria-label="Brief layout"')): html.index('aria-label="Brief layout"')]
    assert "x-data" in toggle


def test_an_unknown_cookie_value_falls_back_to_the_board(client: TestClient) -> None:
    client.cookies.set("brief_view", "<script>")
    html = client.get("/brief").text
    assert 'data-column="priority-1"' in html and "<script>" not in html.split("<main", 1)[1].split("</main>", 1)[0]


def test_lanes_partial_follows_the_same_view(client: TestClient) -> None:
    _task("Exam prep", lane="tasks_due", priority=1)
    assert 'data-column="priority-1"' in client.get("/brief/lanes-partial").json()["html"]
    client.cookies.set("brief_view", "sections")
    assert 'data-column="priority-1"' not in client.get("/brief/lanes-partial").json()["html"]


def test_setting_a_priority_by_hand_locks_it(client: TestClient) -> None:
    item_id = _task("Mine", priority=3)

    response = client.patch(f"/items/{item_id}/priority", json={"priority": 1})

    assert response.status_code == 200
    with db.session() as conn:
        row = conn.execute("SELECT priority, priority_locked FROM items WHERE id = ?", (item_id,)).fetchone()
    assert (row["priority"], row["priority_locked"]) == (1, 1)
    assert item_id not in {t["id"] for t in queries.list_tasks_to_prioritize(_today(), "2000-01-01")}


@pytest.mark.parametrize("priority", [0, 4, "high", None])
def test_priority_route_rejects_anything_but_1_to_3(client: TestClient, priority) -> None:
    item_id = _task("Mine", priority=2)
    assert client.patch(f"/items/{item_id}/priority", json={"priority": priority}).status_code == 422


def test_priority_route_404s_for_an_unknown_task(client: TestClient) -> None:
    assert client.patch("/items/999999/priority", json={"priority": 1}).status_code == 404


def test_priority_route_is_patch_only(client: TestClient) -> None:
    item_id = _task("Mine", priority=2)
    assert client.get(f"/items/{item_id}/priority").status_code in (404, 405)


def test_foreign_host_cannot_change_a_priority(client: TestClient) -> None:
    item_id = _task("Mine", priority=2)
    from command_center.app import app

    response = TestClient(app, base_url=EVIL).patch(f"/items/{item_id}/priority", json={"priority": 1})

    assert response.status_code == 400
    with db.session() as conn:
        assert conn.execute("SELECT priority FROM items WHERE id = ?", (item_id,)).fetchone()["priority"] == 2


def test_adding_a_task_in_a_priority_column_keeps_that_priority(client: TestClient) -> None:
    response = client.post("/items", json={"lane": "action_items", "title": "New high", "priority": 1})

    item_id = response.json()["id"]
    with db.session() as conn:
        row = conn.execute("SELECT priority, priority_locked FROM items WHERE id = ?", (item_id,)).fetchone()
    assert (row["priority"], row["priority_locked"]) == (1, 1)
    assert client.post("/items", json={"lane": "action_items", "title": "x", "priority": 9}).status_code == 422


def test_a_failed_priority_sort_is_said_out_loud(client: TestClient) -> None:
    queries.set_app_value("prioritize_failed_at", "2026-10-22T14:05:00+03:00")
    html = client.get("/brief").text
    assert "Couldn't re-sort priorities at" in html

    queries.set_app_value("prioritize_failed_at", "")
    assert "Couldn't re-sort priorities" not in client.get("/brief").text
