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


# --- queries.py: map_nodes CRUD ----------------------------------------------


def test_create_and_list_map_node(client: TestClient) -> None:
    node_id = queries.create_map_node(kind="target", title="Ship v2", note="by end of month", x=30, y=40)

    nodes = queries.list_map_nodes()

    assert len(nodes) == 1
    assert nodes[0]["id"] == node_id
    assert nodes[0]["kind"] == "target"
    assert nodes[0]["title"] == "Ship v2"
    assert nodes[0]["x"] == 30
    assert nodes[0]["y"] == 40
    assert nodes[0]["project_id"] is None


def test_list_map_nodes_joins_live_project_fields(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path), description="A project")
    queries.update_registered_project(project_id, current_sprint="Sprint 3")
    queries.create_map_node(kind="project", project_id=project_id, x=10, y=10)

    nodes = queries.list_map_nodes()

    assert len(nodes) == 1
    assert nodes[0]["project_id"] == project_id
    assert nodes[0]["project_name"] == "Foo"
    assert nodes[0]["project_current_sprint"] == "Sprint 3"


def test_list_pinnable_projects_excludes_already_pinned(client: TestClient, tmp_path: Path) -> None:
    pinned_id = queries.create_registered_project("Pinned", str(tmp_path))
    unpinned_id = queries.create_registered_project("Unpinned", str(tmp_path))
    queries.create_map_node(kind="project", project_id=pinned_id, x=10, y=10)

    pinnable = queries.list_pinnable_projects()

    assert [p["id"] for p in pinnable] == [unpinned_id]


def test_list_pinnable_projects_excludes_inactive(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    queries.update_registered_project(project_id, active=False)

    assert queries.list_pinnable_projects() == []


def test_update_map_node_position(client: TestClient) -> None:
    node_id = queries.create_map_node(kind="research", title="Read papers", x=10, y=10)

    updated = queries.update_map_node_position(node_id, 55.5, 62.25)

    assert updated is True
    node = queries.list_map_nodes()[0]
    assert node["x"] == 55.5
    assert node["y"] == 62.25


def test_update_map_node_edits_title_note_and_date(client: TestClient) -> None:
    node_id = queries.create_map_node(kind="hackathon", title="Old title", x=10, y=10)

    updated = queries.update_map_node(node_id, title="New title", note="New note", target_date="2026-10-01")

    assert updated is True
    node = queries.list_map_nodes()[0]
    assert node["title"] == "New title"
    assert node["note"] == "New note"
    assert node["target_date"] == "2026-10-01"


def test_update_map_node_refuses_a_project_linked_card(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    node_id = queries.create_map_node(kind="project", project_id=project_id, x=10, y=10)

    updated = queries.update_map_node(node_id, title="Hijacked title", note="", target_date=None)

    assert updated is False
    node = queries.list_map_nodes()[0]
    assert node["title"] == ""  # untouched


def test_delete_map_node_removes_row(client: TestClient) -> None:
    node_id = queries.create_map_node(kind="competition", title="ICPC", x=10, y=10)

    deleted = queries.delete_map_node(node_id)

    assert deleted is True
    assert queries.list_map_nodes() == []


def test_delete_map_node_unpins_project_without_deleting_it(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    node_id = queries.create_map_node(kind="project", project_id=project_id, x=10, y=10)

    queries.delete_map_node(node_id)

    assert queries.list_map_nodes() == []
    assert queries.get_registered_project(project_id) is not None


# --- GET /map ------------------------------------------------------------


def test_map_page_renders_empty_state(client: TestClient) -> None:
    response = client.get("/map")
    assert response.status_code == 200
    assert "Nothing pinned yet" in response.text


def test_map_page_embeds_nodes_and_pinnable_projects(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    queries.create_map_node(kind="target", title="Launch beta", x=20, y=30)

    response = client.get("/map")

    assert response.status_code == 200
    assert "Launch beta" in response.text
    assert f'"id": {project_id}' in response.text or f'"id":{project_id}' in response.text


def test_nav_includes_map_link(client: TestClient) -> None:
    response = client.get("/map")
    assert 'href="/map"' in response.text


# --- POST /map/nodes ------------------------------------------------------


def test_create_target_node(client: TestClient) -> None:
    response = client.post("/map/nodes", json={"kind": "target", "title": "Ship v2", "note": "", "x": 20, "y": 30})

    assert response.status_code == 200
    node_id = response.json()["id"]
    node = queries.list_map_nodes()[0]
    assert node["id"] == node_id
    assert node["kind"] == "target"
    assert node["title"] == "Ship v2"


def test_create_project_node(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))

    response = client.post("/map/nodes", json={"kind": "project", "project_id": project_id})

    assert response.status_code == 200
    node = queries.list_map_nodes()[0]
    assert node["kind"] == "project"
    assert node["project_id"] == project_id


def test_create_project_node_requires_project_id(client: TestClient) -> None:
    response = client.post("/map/nodes", json={"kind": "project"})
    assert response.status_code == 400


def test_create_project_node_404_for_unknown_project(client: TestClient) -> None:
    response = client.post("/map/nodes", json={"kind": "project", "project_id": 99999})
    assert response.status_code == 404


def test_create_node_rejects_unknown_kind(client: TestClient) -> None:
    response = client.post("/map/nodes", json={"kind": "vacation", "title": "Beach"})
    assert response.status_code == 400


def test_create_node_rejects_empty_title(client: TestClient) -> None:
    response = client.post("/map/nodes", json={"kind": "research", "title": "   "})
    assert response.status_code == 400


def test_create_node_rejects_invalid_target_date(client: TestClient) -> None:
    response = client.post("/map/nodes", json={"kind": "hackathon", "title": "HackX", "target_date": "not-a-date"})
    assert response.status_code == 400


def test_create_node_clamps_position(client: TestClient) -> None:
    response = client.post("/map/nodes", json={"kind": "target", "title": "Off grid", "x": 500, "y": -50})

    assert response.status_code == 200
    node = queries.list_map_nodes()[0]
    assert node["x"] == 100.0
    assert node["y"] == 0.0


# --- PATCH /map/nodes/{id}/position ---------------------------------------


def test_patch_position_updates_coordinates(client: TestClient) -> None:
    node_id = queries.create_map_node(kind="target", title="Ship v2", x=10, y=10)

    response = client.patch(f"/map/nodes/{node_id}/position", json={"x": 44.4, "y": 55.5})

    assert response.status_code == 200
    node = queries.list_map_nodes()[0]
    assert node["x"] == 44.4
    assert node["y"] == 55.5


def test_patch_position_404_for_unknown_node(client: TestClient) -> None:
    response = client.patch("/map/nodes/99999/position", json={"x": 10, "y": 10})
    assert response.status_code == 404


# --- PATCH /map/nodes/{id} -------------------------------------------------


def test_patch_node_edits_text_fields(client: TestClient) -> None:
    node_id = queries.create_map_node(kind="research", title="Old", x=10, y=10)

    response = client.patch(
        f"/map/nodes/{node_id}", json={"title": "New", "note": "Updated note", "target_date": "2026-11-01"}
    )

    assert response.status_code == 200
    node = queries.list_map_nodes()[0]
    assert node["title"] == "New"
    assert node["note"] == "Updated note"
    assert node["target_date"] == "2026-11-01"


def test_patch_node_rejects_empty_title(client: TestClient) -> None:
    node_id = queries.create_map_node(kind="research", title="Old", x=10, y=10)
    response = client.patch(f"/map/nodes/{node_id}", json={"title": "  "})
    assert response.status_code == 400


def test_patch_node_404_for_a_project_linked_card(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    node_id = queries.create_map_node(kind="project", project_id=project_id, x=10, y=10)

    response = client.patch(f"/map/nodes/{node_id}", json={"title": "Hijack"})

    assert response.status_code == 404


# --- DELETE /map/nodes/{id} -------------------------------------------------


def test_delete_node(client: TestClient) -> None:
    node_id = queries.create_map_node(kind="competition", title="ICPC", x=10, y=10)

    response = client.delete(f"/map/nodes/{node_id}")

    assert response.status_code == 200
    assert queries.list_map_nodes() == []


def test_delete_node_404_for_unknown_id(client: TestClient) -> None:
    response = client.delete("/map/nodes/99999")
    assert response.status_code == 404
