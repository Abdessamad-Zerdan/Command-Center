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


# --- GET /projects (list) ----------------------------------------------------


def test_projects_page_renders_empty_state(client: TestClient) -> None:
    response = client.get("/projects")
    assert response.status_code == 200
    assert "No projects registered yet" in response.text


def test_projects_page_lists_registered_projects(client: TestClient, tmp_path: Path) -> None:
    queries.create_registered_project("Foo", str(tmp_path), "A test project")

    response = client.get("/projects")

    assert response.status_code == 200
    assert "Foo" in response.text
    assert "A test project" in response.text


# --- POST /projects (register) -----------------------------------------------


def test_register_project_creates_row(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/projects", json={"name": "Foo", "path": str(tmp_path), "description": "desc"}
    )

    assert response.status_code == 200
    project_id = response.json()["id"]
    project = queries.get_registered_project(project_id)
    assert project["name"] == "Foo"
    assert project["path"] == str(tmp_path)
    assert project["active"] == 1


def test_register_project_rejects_empty_name(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/projects", json={"name": "  ", "path": str(tmp_path)})
    assert response.status_code == 400


def test_register_project_rejects_nonexistent_path(client: TestClient) -> None:
    response = client.post("/projects", json={"name": "Foo", "path": "/definitely/not/a/real/path"})
    assert response.status_code == 400


# --- GET /projects/{id} (detail) ---------------------------------------------


def test_project_detail_renders(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    assert "Foo" in response.text


def test_project_detail_404_for_unknown_id(client: TestClient) -> None:
    response = client.get("/projects/99999")
    assert response.status_code == 404


# --- PATCH /projects/{id} -----------------------------------------------------


def test_patch_project_updates_sprint_fields(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))

    response = client.patch(
        f"/projects/{project_id}",
        json={"current_sprint": "Sprint 1", "sprint_goal": "Ship it", "blockers": "None", "target_date": "2026-09-01"},
    )

    assert response.status_code == 200
    project = queries.get_registered_project(project_id)
    assert project["current_sprint"] == "Sprint 1"
    assert project["sprint_goal"] == "Ship it"
    assert project["target_date"] == "2026-09-01"


def test_patch_project_can_deactivate(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))

    response = client.patch(f"/projects/{project_id}", json={"active": False})

    assert response.status_code == 200
    assert queries.get_registered_project(project_id)["active"] == 0


def test_patch_project_404_for_unknown_id(client: TestClient) -> None:
    response = client.patch("/projects/99999", json={"active": False})
    assert response.status_code == 404


# --- DELETE /projects/{id} ----------------------------------------------------


def test_delete_project_removes_row(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))

    response = client.delete(f"/projects/{project_id}")

    assert response.status_code == 200
    assert queries.get_registered_project(project_id) is None


def test_delete_project_orphans_linked_items_instead_of_deleting_them(
    client: TestClient, tmp_path: Path
) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Linked task", project_id=project_id)

    client.delete(f"/projects/{project_id}")

    with db.session() as conn:
        row = conn.execute("SELECT project_id FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row is not None  # task itself survives
    assert row["project_id"] is None  # link is cleared


def test_delete_project_404_for_unknown_id(client: TestClient) -> None:
    response = client.delete("/projects/99999")
    assert response.status_code == 404


# --- GET /projects/{id} — right column (last worked on, linked tasks, files) --


def test_project_detail_collapses_to_single_column_with_nothing_to_show(
    client: TestClient, tmp_path: Path
) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    project_id = queries.create_registered_project("Foo", str(empty_dir))

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    assert "lg:w-2/3" not in response.text
    assert "Last worked on" not in response.text


def test_project_detail_shows_last_worked_on_from_file_mtime(
    client: TestClient, tmp_path: Path
) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "main.py").write_text("print('hi')")
    project_id = queries.create_registered_project("Foo", str(project_dir))

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    assert "Last worked on" in response.text
    assert "main.py" in response.text


def test_project_detail_shows_file_tree(client: TestClient, tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "app.py").write_text("x")
    sub = project_dir / "sub"
    sub.mkdir()
    (sub / "helper.py").write_text("y")
    project_id = queries.create_registered_project("Foo", str(project_dir))

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    assert "app.py" in response.text
    assert "sub/" in response.text
    assert "helper.py" in response.text


def test_project_detail_never_shows_gitignored_credentials_file(
    client: TestClient, tmp_path: Path
) -> None:
    # End-to-end regression for the real bug: credentials.json (and .env)
    # appearing in the rendered page at all, not just in fsutils's own
    # return value.
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".gitignore").write_text("credentials.json\n.env\n")
    (project_dir / "credentials.json").write_text('{"secret": "shh"}')
    (project_dir / ".env").write_text("API_KEY=shh")
    (project_dir / "app.py").write_text("x")
    project_id = queries.create_registered_project("Foo", str(project_dir))

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    assert "credentials.json" not in response.text
    assert "API_KEY=shh" not in response.text  # never read the file's content either
    assert "app.py" in response.text


def test_project_detail_shows_linked_tasks(client: TestClient, tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    project_id = queries.create_registered_project("Foo", str(project_dir))
    queries.create_manual_item("2026-08-14", "urgent", "Linked task", project_id=project_id)

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    assert "Linked task" in response.text
    assert "No tasks linked to this project yet." not in response.text


def test_project_detail_linked_task_card_has_no_height_cap(
    client: TestClient, tmp_path: Path
) -> None:
    # item_card.html's max-h-[180px]/overflow-y-auto is deliberately kept
    # on /brief but must be omitted here (full_height=true) so the card
    # renders at its natural height instead of clipping/scrolling inside
    # an isolated single-card box.
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    project_id = queries.create_registered_project("Foo", str(project_dir))
    queries.create_manual_item("2026-08-14", "urgent", "Linked task", project_id=project_id)

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    # max-h-[180px] is unique to item_card.html's capped state — nothing
    # else on this page uses it (the Files section has its own, unrelated
    # max-h-[50vh]/overflow-y-auto scroll container, so that class alone
    # isn't a safe thing to assert absent here).
    assert "max-h-[180px]" not in response.text


def test_project_detail_shows_file_count_badge_next_to_files(
    client: TestClient, tmp_path: Path
) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "a.py").write_text("x")
    (project_dir / "b.py").write_text("y")
    project_id = queries.create_registered_project("Foo", str(project_dir))

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    assert "Files</span>" in response.text
    assert "&middot; 2</span>" in response.text


def test_project_detail_files_section_is_a_collapsed_disclosure(
    client: TestClient, tmp_path: Path
) -> None:
    # The file tree must start closed (no "open" attribute) — it's
    # reference material you dip into, not the page's focal point.
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "a.py").write_text("x")
    project_id = queries.create_registered_project("Foo", str(project_dir))

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    assert "<details" in response.text
    assert "<details open" not in response.text


def test_project_detail_shows_no_tasks_empty_state(client: TestClient, tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "x.py").write_text("x")  # gives the right column something else to show
    project_id = queries.create_registered_project("Foo", str(project_dir))

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    assert "No tasks linked to this project yet." in response.text


def test_project_detail_active_toggle_reverts_on_a_failed_save(
    client: TestClient, tmp_path: Path
) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    response = client.get(f"/projects/{project_id}")
    assert response.status_code == 200
    assert "this.active = !value" in response.text
