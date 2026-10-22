from pathlib import Path

import pytest

from command_center import db, queries


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _item(source: str, source_id: str, due_date: str | None = None, lane: str = "tasks_due", priority: int = 2) -> dict:
    return {
        "lane": lane,
        "source": source,
        "source_id": source_id,
        "title": "Item",
        "why_it_matters": "",
        "suggested_next_step": "",
        "priority": priority,
        "deep_link": "",
        "due_date": due_date,
    }


# --- save_triage_results / create_manual_item persist due_date -----------


def test_save_triage_results_persists_due_date(isolated_db: None) -> None:
    queries.save_triage_results(
        "2026-08-14", [_item("google_tasks", "t1", due_date="2026-08-20")], [], [], ["tasks"]
    )
    with db.session() as conn:
        row = conn.execute("SELECT due_date FROM items WHERE source_id = 't1'").fetchone()
    assert row["due_date"] == "2026-08-20"


def test_save_triage_results_missing_due_date_key_defaults_to_none(isolated_db: None) -> None:
    item = _item("google_tasks", "t2")
    del item["due_date"]  # simulates a fixtures.py-style dict without the key at all
    queries.save_triage_results("2026-08-14", [item], [], [], ["tasks"])
    with db.session() as conn:
        row = conn.execute("SELECT due_date FROM items WHERE source_id = 't2'").fetchone()
    assert row["due_date"] is None


def test_create_manual_item_with_due_date(isolated_db: None) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Renew passport", due_date="2026-08-21")
    with db.session() as conn:
        row = conn.execute("SELECT due_date FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["due_date"] == "2026-08-21"


def test_create_manual_item_without_due_date_defaults_to_none(isolated_db: None) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "No due date")
    with db.session() as conn:
        row = conn.execute("SELECT due_date FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["due_date"] is None


# --- list_unscheduled_task_candidates ---------------------------------------


def test_list_unscheduled_task_candidates_excludes_non_task_sources(isolated_db: None) -> None:
    queries.save_triage_results("2026-08-14", [_item("gmail", "g1")], [], [], ["gmail"])
    queries.save_triage_results("2026-08-14", [_item("medium", "med1")], [], [], ["medium"])
    queries.create_manual_item("2026-08-14", "urgent", "Manual task")

    candidates = queries.list_unscheduled_task_candidates("2026-12-31")

    assert {c["title"] for c in candidates} == {"Manual task"}


def test_list_unscheduled_task_candidates_excludes_non_pending(isolated_db: None) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Already done")
    queries.set_item_status(item_id, "done")

    candidates = queries.list_unscheduled_task_candidates("2026-12-31")

    assert candidates == []


def test_list_unscheduled_task_candidates_excludes_already_scheduled(isolated_db: None) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Already scheduled")
    queries.schedule_item(item_id, "2026-08-15T09:00:00", "2026-08-15T09:30:00")

    candidates = queries.list_unscheduled_task_candidates("2026-12-31")

    assert candidates == []


def test_list_unscheduled_task_candidates_includes_null_due_date(isolated_db: None) -> None:
    queries.create_manual_item("2026-08-14", "urgent", "No due date")

    candidates = queries.list_unscheduled_task_candidates("2026-08-15")

    assert len(candidates) == 1
    assert candidates[0]["due_date"] is None


def test_list_unscheduled_task_candidates_includes_overdue(isolated_db: None) -> None:
    queries.create_manual_item("2026-08-14", "urgent", "Overdue task", due_date="2026-01-01")

    candidates = queries.list_unscheduled_task_candidates("2026-08-20")

    assert len(candidates) == 1


def test_list_unscheduled_task_candidates_excludes_due_after_week_end(isolated_db: None) -> None:
    queries.create_manual_item("2026-08-14", "urgent", "Far future", due_date="2026-12-25")

    candidates = queries.list_unscheduled_task_candidates("2026-08-20")

    assert candidates == []


# --- schedule_item -----------------------------------------------------------


def test_schedule_item_writes_both_columns(isolated_db: None) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Task to schedule")

    updated = queries.schedule_item(item_id, "2026-08-15T09:00:00", "2026-08-15T09:30:00")

    assert updated is True
    with db.session() as conn:
        row = conn.execute(
            "SELECT scheduled_start, scheduled_end FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    assert row["scheduled_start"] == "2026-08-15T09:00:00"
    assert row["scheduled_end"] == "2026-08-15T09:30:00"


def test_schedule_item_logs_task_event(isolated_db: None) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Task to schedule")

    queries.schedule_item(item_id, "2026-08-15T09:00:00", "2026-08-15T09:30:00")

    with db.session() as conn:
        events = conn.execute("SELECT event_type FROM task_events WHERE task_id = ?", (item_id,)).fetchall()
    assert [e["event_type"] for e in events] == ["created", "scheduled"]


def test_schedule_item_returns_false_for_missing_item(isolated_db: None) -> None:
    assert queries.schedule_item(99999, "2026-08-15T09:00:00", "2026-08-15T09:30:00") is False


def test_schedule_item_returns_false_for_non_pending_item(isolated_db: None) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Done already")
    queries.set_item_status(item_id, "done")

    assert queries.schedule_item(item_id, "2026-08-15T09:00:00", "2026-08-15T09:30:00") is False
