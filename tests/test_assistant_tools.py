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


def test_tool_schemas_have_expected_names_and_required_fields() -> None:
    by_name = {t["function"]["name"]: t["function"] for t in tools.TOOL_SCHEMAS}
    assert set(by_name) == {
        "create_task",
        "update_task",
        "complete_task",
        "move_task_to_date",
        "view_brief",
    }
    assert by_name["create_task"]["parameters"]["required"] == ["title"]
    assert by_name["update_task"]["parameters"]["required"] == ["task_id"]
    assert by_name["complete_task"]["parameters"]["required"] == ["task_id"]
    assert by_name["move_task_to_date"]["parameters"]["required"] == ["item_id", "target_date"]
    assert by_name["view_brief"]["parameters"]["required"] == ["date"]


def test_create_task_schema_has_lane_and_project_id_fields() -> None:
    # Root-cause regression: without these, the model has nowhere to put
    # a stated urgency/lane or project — it gets silently dropped before
    # the confirmation card is even built.
    by_name = {t["function"]["name"]: t["function"] for t in tools.TOOL_SCHEMAS}
    props = by_name["create_task"]["parameters"]["properties"]
    assert "lane" in props
    assert set(props["lane"]["enum"]) == set(tools.LANES)
    assert "project_id" in props
    assert props["project_id"]["type"] == "integer"
    # Neither is required — omitting them must keep today's default
    # behavior (automatic classification, no project link).
    assert by_name["create_task"]["parameters"]["required"] == ["title"]


def test_extract_tasks_schema_is_a_single_scoped_tool() -> None:
    assert len(tools.EXTRACT_TASKS_SCHEMA) == 1
    fn = tools.EXTRACT_TASKS_SCHEMA[0]["function"]
    assert fn["name"] == "extract_tasks"
    assert fn["parameters"]["required"] == ["tasks"]
    item_schema = fn["parameters"]["properties"]["tasks"]["items"]
    assert item_schema["required"] == ["title"]
    assert set(item_schema["properties"]) == {"title", "due_date", "notes"}


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


def test_dispatch_create_task_ignores_lane_and_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # dispatch() only ever talks to the real Google Tasks API, which has
    # no lane/project concept — those args are applied later, locally,
    # by chat.py's post-creation override step, not passed through here.
    calls = []

    def fake_create_task(credentials, tasklist_id, title, notes="", due=""):
        calls.append({"title": title, "notes": notes, "due": due})
        return {"id": "new-id"}

    monkeypatch.setattr(tools.tasks_module, "create_task", fake_create_task)

    tools.dispatch(
        "create_task",
        {"title": "Renew passport", "lane": "urgent", "project_id": 1},
        credentials="fake-creds",
    )

    assert calls[0] == {"title": "Renew passport", "notes": "", "due": ""}


def test_dispatch_create_task_uses_default_tasklist(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_create_task(credentials, tasklist_id, title, notes="", due=""):
        calls.append({"tasklist_id": tasklist_id, "title": title, "notes": notes, "due": due})
        return {"id": "new-id"}

    monkeypatch.setattr(tools.tasks_module, "create_task", fake_create_task)

    result = tools.dispatch(
        "create_task", {"title": "Renew passport", "due_date": "2026-08-21"}, credentials="fake-creds"
    )

    assert result == {"id": "new-id"}
    assert calls[0]["tasklist_id"] == "@default"
    assert calls[0]["title"] == "Renew passport"
    assert calls[0]["due"] == "2026-08-21T00:00:00.000Z"


def test_dispatch_update_task_only_passes_provided_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_update_task(credentials, tasklist_id, task_id, title=None, notes=None, due=None):
        calls.append(
            {"tasklist_id": tasklist_id, "task_id": task_id, "title": title, "notes": notes, "due": due}
        )
        return {"id": task_id}

    monkeypatch.setattr(tools.tasks_module, "update_task", fake_update_task)

    tools.dispatch("update_task", {"task_id": "t1", "title": "New title"}, credentials="fake-creds")

    assert calls[0]["tasklist_id"] == "@default"
    assert calls[0]["task_id"] == "t1"
    assert calls[0]["title"] == "New title"
    assert calls[0]["notes"] is None
    assert calls[0]["due"] is None


def test_dispatch_complete_task(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_complete_task(credentials, tasklist_id, task_id):
        calls.append({"tasklist_id": tasklist_id, "task_id": task_id})
        return {"id": task_id, "status": "completed"}

    monkeypatch.setattr(tools.tasks_module, "complete_task", fake_complete_task)

    tools.dispatch("complete_task", {"task_id": "t1"}, credentials="fake-creds")

    assert calls[0] == {"tasklist_id": "@default", "task_id": "t1"}


def test_dispatch_unknown_tool_raises() -> None:
    with pytest.raises(ValueError):
        tools.dispatch("delete_everything", {}, credentials="fake-creds")


def test_validate_args_requires_date_for_view_brief() -> None:
    assert tools.validate_args("view_brief", {}) is not None
    assert tools.validate_args("view_brief", {"date": "2026-08-16"}) is None


def test_validate_args_rejects_malformed_view_brief_date() -> None:
    assert tools.validate_args("view_brief", {"date": "yesterday"}) is not None
    assert tools.validate_args("view_brief", {"date": "08/16/2026"}) is not None


def test_dispatch_move_task_to_date_ignores_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # Local-DB-only mutation — no Google Tasks call, so passing None for
    # credentials (as chat.py's confirm_action/confirm_batch do for this
    # tool) must not raise or otherwise require it.
    calls = []
    monkeypatch.setattr(
        tools.queries,
        "move_item_to_date",
        lambda item_id, new_brief_date, lane=None: calls.append(
            {"item_id": item_id, "new_brief_date": new_brief_date, "lane": lane}
        )
        or True,
    )

    result = tools.dispatch(
        "move_task_to_date", {"item_id": 42, "target_date": "2026-08-17"}, credentials=None
    )

    assert result == {"moved": True}
    assert calls == [{"item_id": 42, "new_brief_date": "2026-08-17", "lane": None}]


def test_dispatch_move_task_to_date_passes_lane_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        tools.queries,
        "move_item_to_date",
        lambda item_id, new_brief_date, lane=None: calls.append(lane) or True,
    )

    tools.dispatch(
        "move_task_to_date",
        {"item_id": 42, "target_date": "2026-08-17", "lane": "tasks_due"},
        credentials=None,
    )

    assert calls == ["tasks_due"]


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
