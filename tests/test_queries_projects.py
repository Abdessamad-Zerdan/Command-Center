from pathlib import Path

import pytest

from command_center import db, queries


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


# --- registered_projects CRUD -------------------------------------------------


def test_create_and_get_registered_project(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Foo", "/tmp/foo", "A test project")

    project = queries.get_registered_project(project_id)

    assert project["name"] == "Foo"
    assert project["path"] == "/tmp/foo"
    assert project["description"] == "A test project"
    assert project["active"] == 1
    assert project["current_sprint"] is None


def test_get_registered_project_unknown_id_returns_none(isolated_db: None) -> None:
    assert queries.get_registered_project(99999) is None


def test_list_registered_projects_sorted_by_name(isolated_db: None) -> None:
    queries.create_registered_project("Zeta", "/tmp/zeta")
    queries.create_registered_project("Alpha", "/tmp/alpha")

    names = [p["name"] for p in queries.list_registered_projects()]

    assert names == ["Alpha", "Zeta"]


def test_list_registered_projects_active_only(isolated_db: None) -> None:
    active_id = queries.create_registered_project("Active", "/tmp/a")
    inactive_id = queries.create_registered_project("Inactive", "/tmp/b")
    queries.update_registered_project(inactive_id, active=False)

    all_projects = queries.list_registered_projects(active_only=False)
    active_projects = queries.list_registered_projects(active_only=True)

    assert {p["id"] for p in all_projects} == {active_id, inactive_id}
    assert {p["id"] for p in active_projects} == {active_id}


def test_update_registered_project_partial_update_leaves_other_fields(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Foo", "/tmp/foo", "desc")

    updated = queries.update_registered_project(project_id, current_sprint="Sprint 1")

    assert updated is True
    project = queries.get_registered_project(project_id)
    assert project["current_sprint"] == "Sprint 1"
    assert project["description"] == "desc"


def test_update_registered_project_unknown_id_returns_false(isolated_db: None) -> None:
    assert queries.update_registered_project(99999, current_sprint="X") is False


def test_update_registered_project_no_fields_returns_false(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Foo", "/tmp/foo")
    assert queries.update_registered_project(project_id) is False


def test_delete_registered_project_removes_row(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Foo", "/tmp/foo")

    deleted = queries.delete_registered_project(project_id)

    assert deleted is True
    assert queries.get_registered_project(project_id) is None


def test_delete_registered_project_unknown_id_returns_false(isolated_db: None) -> None:
    assert queries.delete_registered_project(99999) is False


def test_delete_registered_project_nulls_linked_items_but_keeps_them(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Foo", "/tmp/foo")
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Linked task", project_id=project_id)

    queries.delete_registered_project(project_id)

    with db.session() as conn:
        row = conn.execute("SELECT project_id, title FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["title"] == "Linked task"
    assert row["project_id"] is None


def test_delete_registered_project_removes_pinned_map_cards(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Foo", "/tmp/foo")
    board_id = queries.create_map_board("Board 1")
    other_board_id = queries.create_map_board("Board 2")
    node_id = queries.create_map_node(board_id, "project", project_id=project_id)
    other_node_id = queries.create_map_node(other_board_id, "project", project_id=project_id)
    label_id = queries.create_map_node(board_id, "label", title="September")
    queries.update_map_node_label_link(node_id, label_id)

    queries.delete_registered_project(project_id)

    assert queries.get_map_node(node_id) is None
    assert queries.get_map_node(other_node_id) is None
    # The label itself is untouched — only the dangling link to it is gone.
    assert queries.get_map_node(label_id) is not None


# --- item <-> project linking --------------------------------------------------


def test_create_manual_item_with_project_id(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Foo", "/tmp/foo")

    item_id = queries.create_manual_item("2026-08-14", "urgent", "Task", project_id=project_id)

    with db.session() as conn:
        row = conn.execute("SELECT project_id FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["project_id"] == project_id


def test_create_manual_item_without_project_id_defaults_to_none(isolated_db: None) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Task")

    with db.session() as conn:
        row = conn.execute("SELECT project_id FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["project_id"] is None


def test_update_item_project_assigns_and_logs_event(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Foo", "/tmp/foo")
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Task")

    updated = queries.update_item_project(item_id, project_id)

    assert updated is True
    with db.session() as conn:
        row = conn.execute("SELECT project_id FROM items WHERE id = ?", (item_id,)).fetchone()
        events = conn.execute(
            "SELECT event_type FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1", (item_id,)
        ).fetchone()
    assert row["project_id"] == project_id
    assert events["event_type"] == "updated"


def test_update_item_project_can_clear_to_none(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Foo", "/tmp/foo")
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Task", project_id=project_id)

    queries.update_item_project(item_id, None)

    with db.session() as conn:
        row = conn.execute("SELECT project_id FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["project_id"] is None


def test_update_item_project_unknown_item_returns_false(isolated_db: None) -> None:
    assert queries.update_item_project(99999, None) is False


def test_list_items_by_project_returns_only_linked_items(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Foo", "/tmp/foo")
    other_project_id = queries.create_registered_project("Bar", "/tmp/bar")
    linked_id = queries.create_manual_item("2026-08-14", "urgent", "Linked", project_id=project_id)
    queries.create_manual_item("2026-08-14", "urgent", "Other project", project_id=other_project_id)
    queries.create_manual_item("2026-08-14", "urgent", "No project")

    items = queries.list_items_by_project(project_id)

    assert [i["id"] for i in items] == [linked_id]
