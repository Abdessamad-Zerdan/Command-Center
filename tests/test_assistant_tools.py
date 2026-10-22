from pathlib import Path

import pytest

from command_center import db
from command_center.assistant import tools


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # describe_pending/describe_done's lane clause reads queries.get_lane_labels(),
    # a real DB call (renamed lanes since Settings > Lane labels) — every test in
    # this file needs an isolated DB, not the machine's real command_center.db.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_validate_args_rejects_unknown_tool() -> None:
    assert tools.validate_args("delete_everything", {}) is not None


def test_validate_args_rejects_non_dict_args() -> None:
    assert tools.validate_args("create_task", None) is not None


def test_validate_args_requires_title_for_create() -> None:
    assert tools.validate_args("create_task", {}) is not None
    assert tools.validate_args("create_task", {"title": "  "}) is not None
    assert tools.validate_args("create_task", {"title": "Renew passport"}) is None


def test_validate_args_requires_task_id_for_update_and_complete() -> None:
    assert tools.validate_args("update_task", {}) is not None
    assert tools.validate_args("update_task", {"task_id": "abc"}) is None
    assert tools.validate_args("complete_task", {}) is not None
    assert tools.validate_args("complete_task", {"task_id": "abc"}) is None


def test_validate_args_rejects_unknown_lane() -> None:
    assert tools.validate_args("create_task", {"title": "X", "lane": "not_a_lane"}) is not None


def test_validate_args_accepts_known_lane_or_no_lane() -> None:
    assert tools.validate_args("create_task", {"title": "X", "lane": "urgent"}) is None
    assert tools.validate_args("create_task", {"title": "X"}) is None


def test_coerce_project_id_handles_int_string_and_junk() -> None:
    assert tools.coerce_project_id(3) == 3
    assert tools.coerce_project_id("3") == 3
    assert tools.coerce_project_id(None) is None
    assert tools.coerce_project_id("not-a-number") is None


def test_validate_args_requires_date_for_view_brief() -> None:
    assert tools.validate_args("view_brief", {}) is not None
    assert tools.validate_args("view_brief", {"date": "2026-08-16"}) is None


def test_validate_args_rejects_malformed_view_brief_date() -> None:
    assert tools.validate_args("view_brief", {"date": "yesterday"}) is not None
    assert tools.validate_args("view_brief", {"date": "08/16/2026"}) is not None


def test_validate_args_requires_item_id_and_target_date_for_move() -> None:
    assert tools.validate_args("move_task_to_date", {}) is not None
    assert tools.validate_args("move_task_to_date", {"item_id": 1}) is not None
    assert tools.validate_args("move_task_to_date", {"target_date": "2026-08-17"}) is not None
    assert tools.validate_args("move_task_to_date", {"item_id": 1, "target_date": "2026-08-17"}) is None


def test_validate_args_rejects_malformed_target_date() -> None:
    assert tools.validate_args("move_task_to_date", {"item_id": 1, "target_date": "tomorrow"}) is not None
    assert tools.validate_args("move_task_to_date", {"item_id": 1, "target_date": "08/17/2026"}) is not None


def test_validate_args_rejects_unknown_lane_for_move() -> None:
    assert (
        tools.validate_args(
            "move_task_to_date", {"item_id": 1, "target_date": "2026-08-17", "lane": "not_a_lane"}
        )
        is not None
    )


def test_describe_pending_move_task_to_date_resolves_title_from_history_lookup() -> None:
    text = tools.describe_pending(
        "move_task_to_date", {"item_id": 42, "target_date": "2026-08-17"}, {42: "Renew passport"}
    )
    assert "Renew passport" in text
    assert "Aug 17" in text


def test_describe_pending_move_task_to_date_falls_back_to_id_when_unknown() -> None:
    text = tools.describe_pending("move_task_to_date", {"item_id": 999, "target_date": "2026-08-17"}, {})
    assert "999" in text


def test_describe_pending_move_task_to_date_shows_lane_when_set() -> None:
    text = tools.describe_pending(
        "move_task_to_date",
        {"item_id": 42, "target_date": "2026-08-17", "lane": "tasks_due"},
        {42: "Renew passport"},
    )
    assert "Tasks Due" in text


def test_describe_done_move_task_to_date() -> None:
    text = tools.describe_done(
        "move_task_to_date", {"item_id": 42, "target_date": "2026-08-17"}, {42: "Renew passport"}
    )
    assert text == "Done — moved 'Renew passport' to Monday, Aug 17."


def test_format_due_date_handles_valid_and_invalid_input() -> None:
    assert tools._format_due_date(None) is None
    assert tools._format_due_date("not-a-date") == "not-a-date"
    formatted = tools._format_due_date("2026-08-21")
    assert "Aug 21" in formatted


def test_to_rfc3339_date_handles_valid_and_invalid_input() -> None:
    assert tools._to_rfc3339_date(None) is None
    assert tools._to_rfc3339_date("not-a-date") is None
    assert tools._to_rfc3339_date("2026-08-21") == "2026-08-21T00:00:00.000Z"


def test_describe_pending_create_task() -> None:
    text = tools.describe_pending(
        "create_task", {"title": "Renew passport", "due_date": "2026-08-21"}, {}
    )
    assert "Renew passport" in text
    assert "Aug 21" in text


def test_describe_pending_create_task_shows_a_renamed_lane_label() -> None:
    from command_center import queries

    queries.set_lane_label("urgent", "Fires")

    text = tools.describe_pending("create_task", {"title": "Renew passport", "lane": "urgent"}, {})

    assert "Fires" in text


def test_describe_pending_create_task_shows_detected_lane() -> None:
    text = tools.describe_pending(
        "create_task", {"title": "Renew passport", "lane": "urgent"}, {}
    )
    assert "Urgent" in text
    assert "lane" in text


def test_describe_pending_create_task_no_lane_mentioned_when_absent() -> None:
    text = tools.describe_pending("create_task", {"title": "Renew passport"}, {})
    assert "lane" not in text


def test_describe_pending_create_task_shows_detected_project() -> None:
    text = tools.describe_pending(
        "create_task",
        {"title": "Update README", "project_id": 1},
        {},
        project_lookup={1: "Daily Command Center"},
    )
    assert "Daily Command Center" in text


def test_describe_pending_create_task_unknown_project_id_omitted() -> None:
    # project_id the model produced doesn't match any project actually in
    # context — degrade to omitting it, never show a wrong/guessed name.
    text = tools.describe_pending(
        "create_task",
        {"title": "Update README", "project_id": 999},
        {},
        project_lookup={1: "Daily Command Center"},
    )
    assert "Daily Command Center" not in text
    assert "999" not in text


def test_describe_done_create_task_shows_lane_and_project() -> None:
    text = tools.describe_done(
        "create_task",
        {"title": "Renew passport", "lane": "urgent", "project_id": 1},
        {},
        project_lookup={1: "Daily Command Center"},
    )
    assert "Urgent" in text
    assert "Daily Command Center" in text


def test_describe_pending_update_task_resolves_title_from_lookup() -> None:
    text = tools.describe_pending(
        "update_task", {"task_id": "t1", "title": "New title"}, {"t1": "Old title"}
    )
    assert "Old title" in text
    assert "New title" in text


def test_describe_pending_complete_task_resolves_title() -> None:
    text = tools.describe_pending("complete_task", {"task_id": "t1"}, {"t1": "Renew SSL cert"})
    assert text == "Mark 'Renew SSL cert' as complete?"


def test_describe_done_messages_mention_done_and_relevant_title() -> None:
    assert "Done" in tools.describe_done("create_task", {"title": "X"}, {})
    assert "Done" in tools.describe_done("update_task", {"task_id": "t1"}, {"t1": "X"})
    result = tools.describe_done("complete_task", {"task_id": "t1"}, {"t1": "Renew SSL cert"})
    assert result == "Done — marked 'Renew SSL cert' as complete."


# --- create_calendar_event -----------------------------------------------------


def test_validate_args_requires_title_and_date_for_calendar_event() -> None:
    assert tools.validate_args("create_calendar_event", {}) is not None
    assert tools.validate_args("create_calendar_event", {"title": "Standup"}) is not None
    assert tools.validate_args("create_calendar_event", {"date": "2026-08-21"}) is not None
    assert (
        tools.validate_args("create_calendar_event", {"title": "Standup", "date": "2026-08-21"}) is None
    )


def test_validate_args_rejects_malformed_calendar_event_date() -> None:
    assert (
        tools.validate_args("create_calendar_event", {"title": "Standup", "date": "tomorrow"})
        is not None
    )


def test_validate_args_rejects_malformed_calendar_event_times() -> None:
    base = {"title": "Standup", "date": "2026-08-21"}
    assert tools.validate_args("create_calendar_event", {**base, "start_time": "9am"}) is not None
    assert tools.validate_args("create_calendar_event", {**base, "end_time": "25:00"}) is not None
    assert tools.validate_args("create_calendar_event", {**base, "start_time": "09:00"}) is None
    assert (
        tools.validate_args(
            "create_calendar_event", {**base, "start_time": "09:00", "end_time": "10:00"}
        )
        is None
    )


def test_event_start_end_all_day_when_no_start_time() -> None:
    start, end = tools._event_start_end("2026-08-21", None, None)
    assert start == {"date": "2026-08-21"}
    assert end == {"date": "2026-08-22"}


def test_event_start_end_timed_defaults_one_hour_when_no_end_time() -> None:
    start, end = tools._event_start_end("2026-08-21", "09:00", None)
    assert start == {"dateTime": "2026-08-21T09:00:00", "timeZone": str(tools.TZ)}
    assert end == {"dateTime": "2026-08-21T10:00:00", "timeZone": str(tools.TZ)}


def test_event_start_end_timed_uses_explicit_end_time() -> None:
    start, end = tools._event_start_end("2026-08-21", "09:00", "11:30")
    assert start["dateTime"] == "2026-08-21T09:00:00"
    assert end["dateTime"] == "2026-08-21T11:30:00"


def test_event_start_end_falls_back_to_one_hour_when_end_before_start() -> None:
    # A model-produced end_time that's before (or equal to) start_time is
    # nonsensical, not a real request for a zero/negative-length event —
    # degrade to the same one-hour default as no end_time at all.
    start, end = tools._event_start_end("2026-08-21", "10:00", "09:00")
    assert end["dateTime"] == "2026-08-21T11:00:00"


def test_describe_pending_create_calendar_event_shows_title_date_and_time() -> None:
    text = tools.describe_pending(
        "create_calendar_event",
        {"title": "Standup", "date": "2026-08-21", "start_time": "09:00", "end_time": "09:30"},
        {},
    )
    assert "Standup" in text
    assert "Aug 21" in text
    assert "09:00" in text
    assert "09:30" in text


def test_describe_pending_create_calendar_event_all_day_has_no_time_clause() -> None:
    text = tools.describe_pending(
        "create_calendar_event", {"title": "Standup", "date": "2026-08-21"}, {}
    )
    assert "Standup" in text
    assert ":" not in text.split("on ")[-1]


def test_describe_done_create_calendar_event() -> None:
    text = tools.describe_done(
        "create_calendar_event", {"title": "Standup", "date": "2026-08-21"}, {}
    )
    assert text == "Done — scheduled 'Standup' on Friday, Aug 21."
