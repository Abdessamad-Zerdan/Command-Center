"""history/work_detail.py and what the reflection now sees: the tasks
finished, where the Pomodoro time went, and what stayed open — not just
counts."""

import json
from datetime import date
from pathlib import Path

import pytest

from command_center import db, queries
from command_center.history import reflection, work_detail

WEEK = (date(2026, 10, 12), date(2026, 10, 19))  # Mon 12 – Sun 18 Oct


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setattr(queries, "get_summary_language", lambda: "English")


def _event(task_id: int, event_type: str, when: str, title: str = "", lane: str = "urgent") -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, event_type, timestamp, metadata_json) VALUES (?, ?, ?, ?)",
            (task_id, event_type, when, json.dumps({"lane": lane, "title": title})),
        )


def _focus(task: str, seconds: int, started: str, status: str = "completed", source_id: int | None = None) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO pomodoro_sessions (task_name, task_source_id, planned_minutes, elapsed_seconds, "
            "started_at, ended_at, status) VALUES (?, ?, 25, ?, ?, ?, ?)",
            (task, source_id, seconds, started, started, status),
        )


def test_completed_tasks_lists_this_periods_finishes_with_their_focus_time() -> None:
    _event(1, "created", "2026-10-10T09:00:00+00:00", "Contract review", "action_items")
    _event(1, "completed", "2026-10-14T15:00:00+00:00")
    _focus("Contract review", 3000, "2026-10-14T10:00:00+03:00", source_id=1)
    _event(2, "created", "2026-10-13T09:00:00+00:00", "Last week's thing")
    _event(2, "completed", "2026-10-20T09:00:00+00:00")  # after the period: not counted

    done = work_detail.completed_tasks(*WEEK)

    assert done == [{"title": "Contract review", "lane": "action_items", "focus_minutes": 50}]


def test_focus_time_totals_sessions_and_ranks_tasks() -> None:
    _focus("Exam prep", 1500, "2026-10-13T10:00:00+03:00")
    _focus("Exam prep", 1500, "2026-10-14T10:00:00+03:00")
    _focus("Email", 600, "2026-10-15T10:00:00+03:00", status="stopped_early")
    _focus("Outside the week", 9999, "2026-10-21T10:00:00+03:00")

    focus = work_detail.focus_time(*WEEK)

    assert (focus["total_minutes"], focus["sessions"], focus["finished_sessions"], focus["stopped_early"]) == (60, 3, 2, 1)
    assert focus["by_task"][0] == {"task": "Exam prep", "minutes": 50, "sessions": 2}


def test_open_all_period_is_what_kept_slipping() -> None:
    _event(1, "created", "2026-10-01T09:00:00+00:00", "Old open thing")              # still open → listed
    _event(2, "created", "2026-10-02T09:00:00+00:00", "Finished during the week")
    _event(2, "completed", "2026-10-15T09:00:00+00:00")                              # done → not listed
    _event(3, "created", "2026-10-14T09:00:00+00:00", "Created inside the week")     # not "all period"
    _event(4, "created", "2026-10-03T09:00:00+00:00", "Dismissed")
    _event(4, "dismissed", "2026-10-05T09:00:00+00:00")                              # let go → not listed

    assert [t["title"] for t in work_detail.open_all_period(*WEEK)] == ["Old open thing"]


def test_a_new_pomodoro_session_marks_a_saved_reflection_out_of_date() -> None:
    reflection._store("week", WEEK[0], "Saved.", reflection._event_count(WEEK[1]))
    assert reflection.get_saved_reflection("week", *WEEK)["stale"] is False

    _focus("Exam prep", 1500, "2026-10-13T10:00:00+03:00")

    saved = reflection.get_saved_reflection("week", *WEEK)
    assert saved["text"] == "Saved." and saved["stale"] is True


def test_no_saved_reflection_and_day_period_give_none() -> None:
    assert reflection.get_saved_reflection("week", *WEEK) is None
    assert reflection.get_saved_reflection("day", date(2026, 10, 14), date(2026, 10, 15)) is None
