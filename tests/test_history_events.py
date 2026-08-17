from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, fixtures, queries
from command_center.assistant import ingest
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def _events(item_id: int | None = None) -> list[dict]:
    with db.session() as conn:
        sql = "SELECT * FROM task_events"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE task_id = ?"
            params = (item_id,)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _seed_item(brief_date: str, lane: str, source: str, source_id: str, title: str) -> int:
    with db.session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO briefs (brief_date, generated_at, degraded_lanes) VALUES (?, ?, '[]')",
            (brief_date, "2026-08-14T10:00:00"),
        )
        cursor = conn.execute(
            "INSERT INTO items (brief_date, lane, source, source_id, title, why_it_matters, "
            "suggested_next_step, priority, deep_link, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, '', '', 2, '', 'pending', ?)",
            (brief_date, lane, source, source_id, title, "2026-08-14T10:00:00"),
        )
        return cursor.lastrowid


# --- set_item_status ---------------------------------------------------


def test_set_item_status_done_logs_completed_event(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-14", "urgent", "gmail", "g1", "Fix the thing")
    queries.set_item_status(item_id, "done")

    events = _events(item_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "completed"
    assert events[0]["metadata_json"]


def test_set_item_status_snoozed_logs_snoozed_event(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-14", "action_items", "gmail", "g2", "Read this")
    queries.set_item_status(item_id, "snoozed", snoozed_until="2026-08-15T00:00:00")

    events = _events(item_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "snoozed"


def test_set_item_status_nonexistent_item_logs_nothing(isolated_db: None) -> None:
    queries.set_item_status(99999, "done")
    assert _events(99999) == []


# --- create_manual_item -------------------------------------------------


def test_create_manual_item_logs_created_event(isolated_db: None) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Renew passport")

    events = _events(item_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "created"
    import json

    meta = json.loads(events[0]["metadata_json"])
    assert meta == {"lane": "urgent", "source": "manual", "title": "Renew passport"}


# --- list_tool_calls / list_task_events ------------------------------------


def test_list_tool_calls_returns_newest_first(isolated_db: None) -> None:
    queries.log_tool_call("create_task", {"title": "A"}, "confirmed")
    queries.log_tool_call("complete_task", {"task_id": "t1"}, "confirmed")

    rows = queries.list_tool_calls()

    assert [r["tool"] for r in rows] == ["complete_task", "create_task"]


def test_list_tool_calls_respects_limit(isolated_db: None) -> None:
    for i in range(3):
        queries.log_tool_call("create_task", {"title": str(i)}, "confirmed")

    rows = queries.list_tool_calls(limit=2)

    assert len(rows) == 2


def test_list_task_events_returns_newest_first(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-14", "urgent", "gmail", "g1", "Fix the thing")
    queries.set_item_status(item_id, "snoozed", snoozed_until="2026-08-15T00:00:00")
    queries.set_item_status(item_id, "done")

    rows = queries.list_task_events()

    assert [r["event_type"] for r in rows] == ["completed", "snoozed"]


# --- reorder_item -----------------------------------------------------------


def test_reorder_item_places_after_a_sibling(isolated_db: None) -> None:
    a = _seed_item("2026-08-14", "action_items", "gmail", "a", "A")
    b = _seed_item("2026-08-14", "action_items", "gmail", "b", "B")
    c = _seed_item("2026-08-14", "action_items", "gmail", "c", "C")

    # Default order (all sort_order=0, tie-broken by id): A, B, C.
    # Move C to sit right after A: A, C, B.
    moved = queries.reorder_item(c, after_item_id=a)

    assert moved is True
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id FROM items WHERE lane = 'action_items' ORDER BY sort_order ASC, id ASC"
        ).fetchall()
    assert [r["id"] for r in rows] == [a, c, b]


def test_reorder_item_with_no_after_id_moves_to_the_front(isolated_db: None) -> None:
    a = _seed_item("2026-08-14", "action_items", "gmail", "a", "A")
    b = _seed_item("2026-08-14", "action_items", "gmail", "b", "B")

    moved = queries.reorder_item(b, after_item_id=None)

    assert moved is True
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id FROM items WHERE lane = 'action_items' ORDER BY sort_order ASC, id ASC"
        ).fetchall()
    assert [r["id"] for r in rows] == [b, a]


def test_reorder_item_scoped_to_same_priority_band(isolated_db: None) -> None:
    # _seed_item's default priority is 2 for all three — bump one to
    # priority 1 directly so it's in a different band.
    a = _seed_item("2026-08-14", "action_items", "gmail", "a", "A")
    b = _seed_item("2026-08-14", "action_items", "gmail", "b", "B")
    with db.session() as conn:
        conn.execute("UPDATE items SET priority = 1 WHERE id = ?", (a,))

    moved = queries.reorder_item(b, after_item_id=a)

    assert moved is False  # a isn't a sibling of b anymore — different priority band


def test_reorder_item_returns_false_for_unknown_item(isolated_db: None) -> None:
    assert queries.reorder_item(99999, after_item_id=None) is False


def test_reorder_item_returns_false_for_unknown_after_item_id(isolated_db: None) -> None:
    a = _seed_item("2026-08-14", "action_items", "gmail", "a", "A")
    assert queries.reorder_item(a, after_item_id=99999) is False


def test_reorder_item_handles_repeated_reorders_correctly(isolated_db: None) -> None:
    a = _seed_item("2026-08-14", "action_items", "gmail", "a", "A")
    b = _seed_item("2026-08-14", "action_items", "gmail", "b", "B")
    c = _seed_item("2026-08-14", "action_items", "gmail", "c", "C")

    queries.reorder_item(c, after_item_id=a)  # A, C, B
    queries.reorder_item(a, after_item_id=b)  # C, B, A

    with db.session() as conn:
        rows = conn.execute(
            "SELECT id FROM items WHERE lane = 'action_items' ORDER BY sort_order ASC, id ASC"
        ).fetchall()
    assert [r["id"] for r in rows] == [c, b, a]


# --- create_synced_task_item ---------------------------------------------


def test_create_synced_task_item_logs_created_event(isolated_db: None) -> None:
    item_id = queries.create_synced_task_item(
        "2026-08-14", "action_items", "Renew passport", "gtask-1"
    )

    with db.session() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["source"] == "google_tasks"
    assert row["source_id"] == "gtask-1"
    assert row["status"] == "pending"

    events = _events(item_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "created"
    import json

    assert json.loads(events[0]["metadata_json"]) == {
        "lane": "action_items",
        "source": "google_tasks",
        "title": "Renew passport",
    }


def test_create_synced_task_item_creates_target_brief_row_if_missing(isolated_db: None) -> None:
    queries.create_synced_task_item("2026-09-01", "action_items", "X", "gtask-2")

    with db.session() as conn:
        row = conn.execute("SELECT * FROM briefs WHERE brief_date = ?", ("2026-09-01",)).fetchone()
    assert row is not None


def test_create_synced_task_item_sets_due_date_and_project_id(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Daily Command Center", "/tmp/dcc")

    item_id = queries.create_synced_task_item(
        "2026-08-14", "action_items", "X", "gtask-3", due_date="2026-08-21", project_id=project_id
    )

    with db.session() as conn:
        row = conn.execute(
            "SELECT due_date, project_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    assert row["due_date"] == "2026-08-21"
    assert row["project_id"] == project_id


# --- update_item_title / update_item_lane -------------------------------


def test_update_item_title_logs_updated_event_for_manual_item(isolated_db: None) -> None:
    item_id = queries.create_manual_item("2026-08-14", "reading", "Old title")
    queries.update_item_title(item_id, "New title")

    events = _events(item_id)
    assert [e["event_type"] for e in events] == ["created", "updated"]


def test_update_item_title_on_non_manual_item_logs_nothing_new(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-14", "reading", "gmail", "g3", "Article")
    updated = queries.update_item_title(item_id, "Renamed")

    assert updated is False
    assert _events(item_id) == []


def test_update_item_lane_logs_updated_event_with_new_lane(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-14", "reading", "gmail", "g4", "Article")
    queries.update_item_lane(item_id, "action_items")

    events = _events(item_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "updated"
    import json

    assert json.loads(events[0]["metadata_json"])["lane"] == "action_items"


# --- update_item_from_task ------------------------------------------------


def test_update_item_from_task_status_done_logs_completed_event(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-14", "tasks_due", "google_tasks", "gt1", "Renew cert")
    updated = queries.update_item_from_task("gt1", status="done")

    assert updated is True
    events = _events(item_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "completed"


def test_update_item_from_task_title_only_logs_updated_event(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-14", "tasks_due", "google_tasks", "gt2", "Old")
    queries.update_item_from_task("gt2", title="New")

    events = _events(item_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "updated"


def test_update_item_from_task_unknown_source_id_logs_nothing(isolated_db: None) -> None:
    updated = queries.update_item_from_task("does-not-exist", status="done")
    assert updated is False
    assert _events() == []


# --- move_item_to_date ---------------------------------------------------


def test_move_item_to_date_updates_brief_date_and_resets_status(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-14", "action_items", "google_tasks", "gt3", "Old task")
    queries.set_item_status(item_id, "done")

    moved = queries.move_item_to_date(item_id, "2026-08-17")

    assert moved is True
    with db.session() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["brief_date"] == "2026-08-17"
    assert row["status"] == "pending"
    assert row["lane"] == "action_items"  # unchanged, no lane override passed


def test_move_item_to_date_can_change_lane_too(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-14", "action_items", "google_tasks", "gt4", "Old task")

    queries.move_item_to_date(item_id, "2026-08-17", lane="tasks_due")

    with db.session() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["lane"] == "tasks_due"


def test_move_item_to_date_creates_target_brief_row_if_missing(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-14", "action_items", "google_tasks", "gt5", "Old task")

    queries.move_item_to_date(item_id, "2026-09-01")

    with db.session() as conn:
        row = conn.execute("SELECT * FROM briefs WHERE brief_date = ?", ("2026-09-01",)).fetchone()
    assert row is not None


def test_move_item_to_date_logs_moved_event(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-14", "action_items", "google_tasks", "gt6", "Old task")

    queries.move_item_to_date(item_id, "2026-08-17")

    events = _events(item_id)
    assert events[-1]["event_type"] == "moved"
    import json

    meta = json.loads(events[-1]["metadata_json"])
    assert meta["to_date"] == "2026-08-17"


def test_move_item_to_date_returns_false_for_unknown_item(isolated_db: None) -> None:
    moved = queries.move_item_to_date(99999, "2026-08-17")
    assert moved is False
    assert _events(99999) == []


# --- list_recent_pending_items --------------------------------------------


def test_list_recent_pending_items_includes_items_in_window(isolated_db: None) -> None:
    _seed_item("2026-08-16", "action_items", "gmail", "r1", "Yesterday's item")

    items = queries.list_recent_pending_items("2026-08-17", days=7)

    assert [i["title"] for i in items] == ["Yesterday's item"]


def test_list_recent_pending_items_excludes_before_date_itself(isolated_db: None) -> None:
    _seed_item("2026-08-17", "action_items", "gmail", "r2", "Today's item")

    items = queries.list_recent_pending_items("2026-08-17", days=7)

    assert items == []


def test_list_recent_pending_items_excludes_outside_the_window(isolated_db: None) -> None:
    _seed_item("2026-08-01", "action_items", "gmail", "r3", "Too old")

    items = queries.list_recent_pending_items("2026-08-17", days=7)

    assert items == []


def test_list_recent_pending_items_excludes_non_pending_status(isolated_db: None) -> None:
    item_id = _seed_item("2026-08-16", "action_items", "gmail", "r4", "Already done")
    queries.set_item_status(item_id, "done")

    items = queries.list_recent_pending_items("2026-08-17", days=7)

    assert items == []


def test_list_recent_pending_items_orders_newest_day_first(isolated_db: None) -> None:
    _seed_item("2026-08-14", "action_items", "gmail", "r5", "Older")
    _seed_item("2026-08-16", "action_items", "gmail", "r6", "Newer")

    items = queries.list_recent_pending_items("2026-08-17", days=7)

    assert [i["title"] for i in items] == ["Newer", "Older"]


# --- save_triage_results -------------------------------------------------


def _item(source: str, source_id: str, lane: str = "urgent") -> dict:
    return {
        "lane": lane,
        "source": source,
        "source_id": source_id,
        "title": "Item",
        "why_it_matters": "",
        "suggested_next_step": "",
        "priority": 2,
        "deep_link": "",
    }


def test_save_triage_results_logs_created_for_genuinely_new_items(isolated_db: None) -> None:
    queries.save_triage_results("2026-08-14", [_item("gmail", "m1")], [], [], ["gmail"])

    with db.session() as conn:
        item_id = conn.execute(
            "SELECT id FROM items WHERE source='gmail' AND source_id='m1'"
        ).fetchone()["id"]
    events = _events(item_id)
    assert [e["event_type"] for e in events] == ["created"]


def test_save_triage_results_duplicate_insert_or_ignore_logs_nothing_more(isolated_db: None) -> None:
    queries.save_triage_results("2026-08-14", [_item("gmail", "m2")], [], [], ["gmail"])
    queries.save_triage_results("2026-08-14", [_item("gmail", "m2")], [], [], ["gmail"])

    with db.session() as conn:
        item_id = conn.execute(
            "SELECT id FROM items WHERE source='gmail' AND source_id='m2'"
        ).fetchone()["id"]
    events = _events(item_id)
    assert len(events) == 1


def test_save_triage_results_force_replace_of_new_pair_logs_created(isolated_db: None) -> None:
    queries.save_triage_results("2026-08-14", [_item("gmail", "m3")], [], [], ["gmail"], force=True)

    with db.session() as conn:
        item_id = conn.execute(
            "SELECT id FROM items WHERE source='gmail' AND source_id='m3'"
        ).fetchone()["id"]
    assert len(_events(item_id)) == 1


def test_save_triage_results_force_replace_of_existing_pair_logs_nothing(isolated_db: None) -> None:
    queries.save_triage_results("2026-08-14", [_item("gmail", "m4")], [], [], ["gmail"])
    with db.session() as conn:
        old_id = conn.execute(
            "SELECT id FROM items WHERE source='gmail' AND source_id='m4'"
        ).fetchone()["id"]

    queries.save_triage_results("2026-08-14", [_item("gmail", "m4")], [], [], ["gmail"], force=True)

    with db.session() as conn:
        new_id = conn.execute(
            "SELECT id FROM items WHERE source='gmail' AND source_id='m4'"
        ).fetchone()["id"]

    assert new_id != old_id  # INSERT OR REPLACE really does give it a new id
    # No new task_events rows anywhere — one 'created' from the first call, none since.
    with db.session() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM task_events").fetchone()["n"]
    assert total == 1


# --- fixtures.seed() demo-mode boundary ----------------------------------


def test_fixture_seed_never_logs_task_events(isolated_db: None) -> None:
    fixtures.seed()
    fixtures.seed()  # idempotent re-run, e.g. every app restart in demo mode

    assert _events() == []


def test_fixture_seed_flags_the_brief_as_fixture_data(isolated_db: None) -> None:
    fixtures.seed()

    from command_center import queries

    today = queries.list_history_dates()[0]
    with db.session() as conn:
        row = conn.execute(
            "SELECT is_fixture FROM briefs WHERE brief_date = ?", (today,)
        ).fetchone()
    assert row["is_fixture"] == 1


# --- route-level: hooks fire through the real HTTP layer ------------------


def test_mark_done_route_logs_completed_event(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Do the thing")

    response = client.post(f"/items/{item_id}/done")

    assert response.status_code == 200
    events = _events(item_id)
    assert [e["event_type"] for e in events] == ["created", "completed"]


def test_mark_snoozed_route_logs_snoozed_event(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Do the thing later")

    response = client.post(f"/items/{item_id}/snooze")

    assert response.status_code == 200
    events = _events(item_id)
    assert [e["event_type"] for e in events] == ["created", "snoozed"]


def test_create_item_route_logs_created_event(client: TestClient) -> None:
    response = client.post("/items", json={"lane": "reading", "title": "Read this"})
    assert response.status_code == 200
    item_id = response.json()["id"]

    events = _events(item_id)
    assert [e["event_type"] for e in events] == ["created"]


def test_update_item_title_route_logs_updated_event(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-08-14", "reading", "Old")

    response = client.patch(f"/items/{item_id}/title", json={"title": "New"})

    assert response.status_code == 200
    events = _events(item_id)
    assert [e["event_type"] for e in events] == ["created", "updated"]


def test_update_item_lane_route_logs_updated_event(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-08-14", "reading", "Move me")

    response = client.patch(f"/items/{item_id}/lane", json={"lane": "urgent"})

    assert response.status_code == 200
    events = _events(item_id)
    assert [e["event_type"] for e in events] == ["created", "updated"]
