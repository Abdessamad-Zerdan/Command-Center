from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, fixtures, queries
from command_center.assistant import ingest
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(ingest, "rebuild_index", lambda *a, **k: False)
    monkeypatch.setattr(fixtures, "seed", lambda: None)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def board_id(client: TestClient) -> int:
    return queries.create_map_board("September")


# --- queries.py: map_boards CRUD ----------------------------------------------


def test_create_and_list_map_board(client: TestClient) -> None:
    board_id = queries.create_map_board("September")

    boards = queries.list_map_boards()

    assert len(boards) == 1
    assert boards[0]["id"] == board_id
    assert boards[0]["name"] == "September"


def test_get_or_create_default_board_creates_one_when_none_exist(client: TestClient) -> None:
    board = queries.get_or_create_default_board()
    assert board["name"] == "Board 1"
    assert queries.list_map_boards() == [board]


def test_get_or_create_default_board_reuses_existing(client: TestClient) -> None:
    first = queries.create_map_board("September")
    board = queries.get_or_create_default_board()
    assert board["id"] == first


def test_rename_map_board(client: TestClient, board_id: int) -> None:
    updated = queries.rename_map_board(board_id, "October")
    assert updated is True
    assert queries.get_map_board(board_id)["name"] == "October"


def test_rename_map_board_404_for_unknown_id(client: TestClient) -> None:
    assert queries.rename_map_board(99999, "October") is False


def test_delete_map_board_removes_its_nodes_too(client: TestClient, board_id: int) -> None:
    queries.create_map_node(board_id=board_id, kind="target", title="Ship v2", x=10, y=10)

    deleted = queries.delete_map_board(board_id)

    assert deleted is True
    assert queries.get_map_board(board_id) is None
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM map_nodes WHERE board_id = ?", (board_id,)).fetchall()
    assert rows == []


# --- queries.py: map_nodes CRUD (board-scoped) --------------------------------


def test_create_and_list_map_node(client: TestClient, board_id: int) -> None:
    node_id = queries.create_map_node(board_id=board_id, kind="target", title="Ship v2", note="by end of month", x=30, y=40)

    nodes = queries.list_map_nodes(board_id)

    assert len(nodes) == 1
    assert nodes[0]["id"] == node_id
    assert nodes[0]["board_id"] == board_id
    assert nodes[0]["kind"] == "target"
    assert nodes[0]["title"] == "Ship v2"
    assert nodes[0]["x"] == 30
    assert nodes[0]["y"] == 40
    assert nodes[0]["project_id"] is None


def test_list_map_nodes_is_scoped_to_its_board(client: TestClient) -> None:
    sept = queries.create_map_board("September")
    october = queries.create_map_board("October")
    queries.create_map_node(board_id=sept, kind="target", title="Sept goal", x=10, y=10)
    queries.create_map_node(board_id=october, kind="target", title="Oct goal", x=10, y=10)

    assert [n["title"] for n in queries.list_map_nodes(sept)] == ["Sept goal"]
    assert [n["title"] for n in queries.list_map_nodes(october)] == ["Oct goal"]


def test_list_map_nodes_joins_live_project_fields(client: TestClient, board_id: int, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path), description="A project")
    queries.update_registered_project(project_id, current_sprint="Sprint 3")
    queries.create_map_node(board_id=board_id, kind="project", project_id=project_id, x=10, y=10)

    nodes = queries.list_map_nodes(board_id)

    assert len(nodes) == 1
    assert nodes[0]["project_id"] == project_id
    assert nodes[0]["project_name"] == "Foo"
    assert nodes[0]["project_current_sprint"] == "Sprint 3"


def test_list_pinnable_projects_excludes_already_pinned_on_that_board(
    client: TestClient, board_id: int, tmp_path: Path
) -> None:
    pinned_id = queries.create_registered_project("Pinned", str(tmp_path))
    unpinned_id = queries.create_registered_project("Unpinned", str(tmp_path))
    queries.create_map_node(board_id=board_id, kind="project", project_id=pinned_id, x=10, y=10)

    pinnable = queries.list_pinnable_projects(board_id)

    assert [p["id"] for p in pinnable] == [unpinned_id]


def test_list_pinnable_projects_lets_the_same_project_pin_on_multiple_boards(
    client: TestClient, tmp_path: Path
) -> None:
    sept = queries.create_map_board("September")
    october = queries.create_map_board("October")
    project_id = queries.create_registered_project("Ongoing", str(tmp_path))
    queries.create_map_node(board_id=sept, kind="project", project_id=project_id, x=10, y=10)

    # Still pinnable on October, even though already pinned on September.
    assert [p["id"] for p in queries.list_pinnable_projects(october)] == [project_id]


def test_list_pinnable_projects_excludes_inactive(client: TestClient, board_id: int, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    queries.update_registered_project(project_id, active=False)

    assert queries.list_pinnable_projects(board_id) == []


def test_update_map_node_position(client: TestClient, board_id: int) -> None:
    node_id = queries.create_map_node(board_id=board_id, kind="research", title="Read papers", x=10, y=10)

    updated = queries.update_map_node_position(node_id, 55.5, 62.25)

    assert updated is True
    node = queries.list_map_nodes(board_id)[0]
    assert node["x"] == 55.5
    assert node["y"] == 62.25


def test_update_map_node_edits_title_note_date_and_label_link(client: TestClient, board_id: int) -> None:
    label_id = queries.create_map_node(board_id=board_id, kind="label", title="September", x=5, y=5)
    node_id = queries.create_map_node(board_id=board_id, kind="hackathon", title="Old title", x=10, y=10)

    updated = queries.update_map_node(
        node_id, title="New title", note="New note", target_date="2026-10-01", linked_label_id=label_id
    )

    assert updated is True
    node = next(n for n in queries.list_map_nodes(board_id) if n["id"] == node_id)
    assert node["title"] == "New title"
    assert node["note"] == "New note"
    assert node["target_date"] == "2026-10-01"
    assert node["linked_label_id"] == label_id


def test_update_map_node_refuses_a_project_linked_card(client: TestClient, board_id: int, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    node_id = queries.create_map_node(board_id=board_id, kind="project", project_id=project_id, x=10, y=10)

    updated = queries.update_map_node(node_id, title="Hijacked title", note="", target_date=None, linked_label_id=None)

    assert updated is False
    node = queries.list_map_nodes(board_id)[0]
    assert node["title"] == ""  # untouched


def test_delete_map_node_removes_row(client: TestClient, board_id: int) -> None:
    node_id = queries.create_map_node(board_id=board_id, kind="competition", title="ICPC", x=10, y=10)

    deleted = queries.delete_map_node(node_id)

    assert deleted is True
    assert queries.list_map_nodes(board_id) == []


def test_delete_map_node_unpins_project_without_deleting_it(client: TestClient, board_id: int, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    node_id = queries.create_map_node(board_id=board_id, kind="project", project_id=project_id, x=10, y=10)

    queries.delete_map_node(node_id)

    assert queries.list_map_nodes(board_id) == []
    assert queries.get_registered_project(project_id) is not None


def test_delete_map_node_unlinks_cards_pointing_at_a_deleted_label(client: TestClient, board_id: int) -> None:
    label_id = queries.create_map_node(board_id=board_id, kind="label", title="September", x=5, y=5)
    node_id = queries.create_map_node(
        board_id=board_id, kind="target", title="Ship v2", linked_label_id=label_id, x=10, y=10
    )

    queries.delete_map_node(label_id)

    node = queries.list_map_nodes(board_id)[0]
    assert node["id"] == node_id
    assert node["linked_label_id"] is None


def test_list_labels_on_board(client: TestClient, board_id: int) -> None:
    queries.create_map_node(board_id=board_id, kind="label", title="September", x=5, y=5)
    queries.create_map_node(board_id=board_id, kind="target", title="Not a label", x=10, y=10)

    labels = queries.list_labels_on_board(board_id)

    assert len(labels) == 1
    assert labels[0]["title"] == "September"


# --- GET /map ------------------------------------------------------------


def test_map_page_creates_and_uses_a_default_board(client: TestClient) -> None:
    response = client.get("/map")
    assert response.status_code == 200
    assert "Board 1" in response.text
    assert "Nothing pinned yet" in response.text


def test_map_page_with_board_param_shows_that_boards_nodes(client: TestClient) -> None:
    sept = queries.create_map_board("September")
    october = queries.create_map_board("October")
    queries.create_map_node(board_id=sept, kind="target", title="Sept-only card", x=20, y=30)

    response = client.get(f"/map?board={sept}")
    assert "Sept-only card" in response.text

    response = client.get(f"/map?board={october}")
    assert "Sept-only card" not in response.text


def test_map_page_falls_back_to_default_for_unknown_board(client: TestClient) -> None:
    response = client.get("/map?board=99999", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/map"


def test_nav_includes_map_link(client: TestClient) -> None:
    response = client.get("/map")
    assert 'href="/map"' in response.text


# --- POST /map/boards ------------------------------------------------------


def test_create_board(client: TestClient) -> None:
    response = client.post("/map/boards", json={"name": "October"})
    assert response.status_code == 200
    board_id = response.json()["id"]
    assert queries.get_map_board(board_id)["name"] == "October"


def test_create_board_rejects_empty_name(client: TestClient) -> None:
    response = client.post("/map/boards", json={"name": "   "})
    assert response.status_code == 400


def test_rename_board_route(client: TestClient, board_id: int) -> None:
    response = client.patch(f"/map/boards/{board_id}", json={"name": "October"})
    assert response.status_code == 200
    assert queries.get_map_board(board_id)["name"] == "October"


def test_rename_board_route_404(client: TestClient) -> None:
    response = client.patch("/map/boards/99999", json={"name": "October"})
    assert response.status_code == 404


def test_delete_board_route(client: TestClient, board_id: int) -> None:
    response = client.delete(f"/map/boards/{board_id}")
    assert response.status_code == 200
    assert queries.get_map_board(board_id) is None


def test_delete_board_route_404(client: TestClient) -> None:
    response = client.delete("/map/boards/99999")
    assert response.status_code == 404


# --- POST /map/nodes ------------------------------------------------------


def test_create_target_node(client: TestClient, board_id: int) -> None:
    response = client.post(
        "/map/nodes", json={"board_id": board_id, "kind": "target", "title": "Ship v2", "note": "", "x": 20, "y": 30}
    )

    assert response.status_code == 200
    node_id = response.json()["id"]
    node = queries.list_map_nodes(board_id)[0]
    assert node["id"] == node_id
    assert node["kind"] == "target"
    assert node["title"] == "Ship v2"


def test_create_node_404_for_unknown_board(client: TestClient) -> None:
    response = client.post("/map/nodes", json={"board_id": 99999, "kind": "target", "title": "Ship v2"})
    assert response.status_code == 404


def test_create_label_node(client: TestClient, board_id: int) -> None:
    response = client.post("/map/nodes", json={"board_id": board_id, "kind": "label", "title": "September"})
    assert response.status_code == 200
    node = queries.list_map_nodes(board_id)[0]
    assert node["kind"] == "label"
    assert node["title"] == "September"


def test_create_node_with_a_valid_label_link(client: TestClient, board_id: int) -> None:
    label_id = queries.create_map_node(board_id=board_id, kind="label", title="September", x=5, y=5)

    response = client.post(
        "/map/nodes",
        json={"board_id": board_id, "kind": "hackathon", "title": "HackX", "linked_label_id": label_id},
    )

    assert response.status_code == 200
    node = next(n for n in queries.list_map_nodes(board_id) if n["kind"] == "hackathon")
    assert node["linked_label_id"] == label_id


def test_create_node_rejects_a_label_from_a_different_board(client: TestClient) -> None:
    sept = queries.create_map_board("September")
    october = queries.create_map_board("October")
    other_label_id = queries.create_map_node(board_id=october, kind="label", title="October", x=5, y=5)

    response = client.post(
        "/map/nodes",
        json={"board_id": sept, "kind": "hackathon", "title": "HackX", "linked_label_id": other_label_id},
    )

    assert response.status_code == 400


def test_create_project_node(client: TestClient, board_id: int, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))

    response = client.post("/map/nodes", json={"board_id": board_id, "kind": "project", "project_id": project_id})

    assert response.status_code == 200
    node = queries.list_map_nodes(board_id)[0]
    assert node["kind"] == "project"
    assert node["project_id"] == project_id


def test_create_project_node_requires_project_id(client: TestClient, board_id: int) -> None:
    response = client.post("/map/nodes", json={"board_id": board_id, "kind": "project"})
    assert response.status_code == 400


def test_create_project_node_404_for_unknown_project(client: TestClient, board_id: int) -> None:
    response = client.post("/map/nodes", json={"board_id": board_id, "kind": "project", "project_id": 99999})
    assert response.status_code == 404


def test_create_node_rejects_unknown_kind(client: TestClient, board_id: int) -> None:
    response = client.post("/map/nodes", json={"board_id": board_id, "kind": "vacation", "title": "Beach"})
    assert response.status_code == 400


def test_create_node_rejects_empty_title(client: TestClient, board_id: int) -> None:
    response = client.post("/map/nodes", json={"board_id": board_id, "kind": "research", "title": "   "})
    assert response.status_code == 400


def test_create_node_rejects_invalid_target_date(client: TestClient, board_id: int) -> None:
    response = client.post(
        "/map/nodes", json={"board_id": board_id, "kind": "hackathon", "title": "HackX", "target_date": "not-a-date"}
    )
    assert response.status_code == 400


def test_create_node_clamps_position(client: TestClient, board_id: int) -> None:
    response = client.post("/map/nodes", json={"board_id": board_id, "kind": "target", "title": "Off grid", "x": 500, "y": -50})

    assert response.status_code == 200
    node = queries.list_map_nodes(board_id)[0]
    assert node["x"] == 100.0
    assert node["y"] == 0.0


# --- PATCH /map/nodes/{id}/position ---------------------------------------


def test_patch_position_updates_coordinates(client: TestClient, board_id: int) -> None:
    node_id = queries.create_map_node(board_id=board_id, kind="target", title="Ship v2", x=10, y=10)

    response = client.patch(f"/map/nodes/{node_id}/position", json={"x": 44.4, "y": 55.5})

    assert response.status_code == 200
    node = queries.list_map_nodes(board_id)[0]
    assert node["x"] == 44.4
    assert node["y"] == 55.5


def test_patch_position_404_for_unknown_node(client: TestClient) -> None:
    response = client.patch("/map/nodes/99999/position", json={"x": 10, "y": 10})
    assert response.status_code == 404


# --- PATCH /map/nodes/{id} -------------------------------------------------


def test_patch_node_edits_text_fields(client: TestClient, board_id: int) -> None:
    node_id = queries.create_map_node(board_id=board_id, kind="research", title="Old", x=10, y=10)

    response = client.patch(
        f"/map/nodes/{node_id}", json={"title": "New", "note": "Updated note", "target_date": "2026-11-01"}
    )

    assert response.status_code == 200
    node = queries.list_map_nodes(board_id)[0]
    assert node["title"] == "New"
    assert node["note"] == "Updated note"
    assert node["target_date"] == "2026-11-01"


def test_patch_node_sets_a_label_link(client: TestClient, board_id: int) -> None:
    label_id = queries.create_map_node(board_id=board_id, kind="label", title="September", x=5, y=5)
    node_id = queries.create_map_node(board_id=board_id, kind="research", title="Old", x=10, y=10)

    response = client.patch(f"/map/nodes/{node_id}", json={"title": "Old", "linked_label_id": label_id})

    assert response.status_code == 200
    node = next(n for n in queries.list_map_nodes(board_id) if n["id"] == node_id)
    assert node["linked_label_id"] == label_id


def test_patch_node_rejects_a_label_from_a_different_board(client: TestClient) -> None:
    sept = queries.create_map_board("September")
    october = queries.create_map_board("October")
    other_label_id = queries.create_map_node(board_id=october, kind="label", title="October", x=5, y=5)
    node_id = queries.create_map_node(board_id=sept, kind="research", title="Old", x=10, y=10)

    response = client.patch(f"/map/nodes/{node_id}", json={"title": "Old", "linked_label_id": other_label_id})

    assert response.status_code == 400


def test_patch_node_rejects_empty_title(client: TestClient, board_id: int) -> None:
    node_id = queries.create_map_node(board_id=board_id, kind="research", title="Old", x=10, y=10)
    response = client.patch(f"/map/nodes/{node_id}", json={"title": "  "})
    assert response.status_code == 400


def test_patch_node_404_for_a_project_linked_card(client: TestClient, board_id: int, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    node_id = queries.create_map_node(board_id=board_id, kind="project", project_id=project_id, x=10, y=10)

    response = client.patch(f"/map/nodes/{node_id}", json={"title": "Hijack"})

    assert response.status_code == 404


def test_patch_node_404_for_unknown_node(client: TestClient) -> None:
    response = client.patch("/map/nodes/99999", json={"title": "New"})
    assert response.status_code == 404


# --- DELETE /map/nodes/{id} -------------------------------------------------


def test_delete_node(client: TestClient, board_id: int) -> None:
    node_id = queries.create_map_node(board_id=board_id, kind="competition", title="ICPC", x=10, y=10)

    response = client.delete(f"/map/nodes/{node_id}")

    assert response.status_code == 200
    assert queries.list_map_nodes(board_id) == []


def test_delete_node_404_for_unknown_id(client: TestClient) -> None:
    response = client.delete("/map/nodes/99999")
    assert response.status_code == 404
