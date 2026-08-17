from pathlib import Path

import pytest

from command_center import auth, db, pipeline, queries, triage
from command_center.assistant import chat, retrieval, tools


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _no_tool_calls(content: str):
    return lambda messages, tools: {"content": content, "tool_calls": []}


def test_live_brief_summary_reports_no_brief_yet(isolated_db: None) -> None:
    assert chat._live_brief_summary() == "No brief has been generated yet today."


def test_live_brief_summary_lists_pending_items_by_lane(isolated_db: None) -> None:
    today = chat._today()
    queries.create_manual_item(today, "urgent", "Fix the thing")
    queries.create_manual_item(today, "tasks_due", "Renew cert")

    summary = chat._live_brief_summary()

    assert "urgent: Fix the thing" in summary
    assert "tasks_due: Renew cert" in summary


def test_live_brief_summary_reports_no_pending_items(isolated_db: None) -> None:
    today = chat._today()
    item_id = queries.create_manual_item(today, "urgent", "Already done")
    queries.set_item_status(item_id, "done")

    assert chat._live_brief_summary() == "No pending items today."


def test_live_task_context_reports_no_tasks_available(isolated_db: None) -> None:
    text, lookup = chat._live_task_context()
    assert text == "No tasks available."
    assert lookup == {}


def test_recent_history_context_reports_no_items(isolated_db: None) -> None:
    text, lookup = chat._recent_history_context()
    assert "No pending items" in text
    assert lookup == {}


def test_recent_history_context_lists_items_within_window(isolated_db: None) -> None:
    from datetime import date, timedelta

    yesterday = (date.fromisoformat(chat._today()) - timedelta(days=1)).isoformat()
    with db.session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO briefs (brief_date, generated_at, degraded_lanes) VALUES (?, ?, '[]')",
            (yesterday, "2026-08-16T10:00:00"),
        )
        cursor = conn.execute(
            "INSERT INTO items (brief_date, lane, source, source_id, title, why_it_matters, "
            "suggested_next_step, priority, deep_link, status, created_at) "
            "VALUES (?, 'action_items', 'google_tasks', 'gt1', 'Old task', '', '', 2, '', 'pending', ?)",
            (yesterday, "2026-08-16T10:00:00"),
        )
        item_id = cursor.lastrowid

    text, lookup = chat._recent_history_context()

    assert "Old task" in text
    assert lookup == {item_id: "Old task"}


def test_project_context_reports_none_registered(isolated_db: None) -> None:
    text, lookup = chat._project_context()
    assert text == "No registered projects."
    assert lookup == {}


def test_project_context_lists_active_projects(isolated_db: None) -> None:
    project_id = queries.create_registered_project("Daily Command Center", "/tmp/dcc")
    text, lookup = chat._project_context()
    assert f"{project_id}: Daily Command Center" in text
    assert lookup == {project_id: "Daily Command Center"}


def test_answer_includes_registered_projects_in_prompt(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries.create_registered_project("Daily Command Center", "/tmp/dcc")
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    captured = {}

    def _fake(messages, tools):
        captured["messages"] = messages
        return {"content": "answer", "tool_calls": []}

    monkeypatch.setattr(triage, "run_groq_chat_with_tools", _fake)

    chat.answer("anything")

    user_message = captured["messages"][1]["content"]
    assert "Daily Command Center" in user_message


def test_answer_builds_grounded_prompt_and_returns_sources(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retrieval,
        "top_k",
        lambda query, k=5: [
            {
                "source": "vision",
                "section": "Current Focus",
                "content": "Building a brief app.",
                "score": 0.9,
            },
            {"source": "profile", "section": "Bio", "content": "Ada — Engineer.", "score": 0.8},
        ],
    )
    captured = {}

    def _fake(messages, tools):
        captured["messages"] = messages
        return {"content": "You're building a brief app.", "tool_calls": []}

    monkeypatch.setattr(triage, "run_groq_chat_with_tools", _fake)

    result = chat.answer("What am I building?")

    assert result["answer"] == "You're building a brief app."
    assert result["sources"] == ["Current Focus", "Bio"]
    system_message = captured["messages"][0]["content"]
    assert "only the context provided" in system_message
    user_message = captured["messages"][1]["content"]
    assert "Building a brief app." in user_message
    assert "What am I building?" in user_message


def test_answer_tells_the_model_todays_date_for_relative_date_resolution(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Groq has no built-in awareness of "today" — without this, "next
    # Friday" resolves against the model's training data instead of the
    # real calendar (confirmed live: it picked a date months off).
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    captured = {}

    def _fake(messages, tools):
        captured["messages"] = messages
        return {"content": "answer", "tool_calls": []}

    monkeypatch.setattr(triage, "run_groq_chat_with_tools", _fake)

    chat.answer("create a task due next Friday")

    user_message = captured["messages"][1]["content"]
    assert "Today's date is" in user_message
    assert chat._today() in user_message


def test_answer_dedupes_sources_preserving_order(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retrieval,
        "top_k",
        lambda query, k=5: [
            {"source": "vision", "section": "Skills", "content": "Python.", "score": 0.9},
            {"source": "vision", "section": "Skills", "content": "More Python.", "score": 0.85},
        ],
    )
    monkeypatch.setattr(triage, "run_groq_chat_with_tools", _no_tool_calls("answer"))

    result = chat.answer("What are my skills?")

    assert result["sources"] == ["Skills"]


def test_answer_propagates_provider_errors(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])

    def _raise(messages, tools):
        raise triage.TriageProviderError("Groq request failed: authentication error.")

    monkeypatch.setattr(triage, "run_groq_chat_with_tools", _raise)

    with pytest.raises(triage.TriageProviderError, match="authentication error"):
        chat.answer("anything")


def test_answer_returns_pending_action_for_a_valid_tool_call(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [
                {
                    "name": "create_task",
                    "arguments": {"title": "Renew passport", "due_date": "2026-08-21"},
                }
            ],
        },
    )

    result = chat.answer("create a task to renew my passport due next Friday")

    assert "pending_action" in result
    assert result["pending_action"]["tool"] == "create_task"
    assert result["pending_action"]["args"]["title"] == "Renew passport"
    assert "Renew passport" in result["pending_action"]["confirmation_text"]

    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "proposed"
    assert rows[0]["tool"] == "create_task"


def test_answer_navigates_for_a_view_brief_call_to_a_past_day_with_a_brief(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date, timedelta

    yesterday = (date.fromisoformat(chat._today()) - timedelta(days=1)).isoformat()
    queries.create_manual_item(yesterday, "urgent", "Old task")

    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [{"name": "view_brief", "arguments": {"date": yesterday}}],
        },
    )

    result = chat.answer("take me to yesterday's brief")

    assert result["navigate"] == f"/history/{yesterday}"
    assert "pending_action" not in result
    assert "pending_batch" not in result


def test_answer_navigates_to_slash_brief_for_todays_own_date(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = chat._today()
    queries.create_manual_item(today, "urgent", "Today task")

    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [{"name": "view_brief", "arguments": {"date": today}}],
        },
    )

    result = chat.answer("take me to today's brief")

    assert result["navigate"] == "/brief"


def test_answer_view_brief_with_no_brief_for_that_date_gives_plain_message(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [{"name": "view_brief", "arguments": {"date": "2020-01-01"}}],
        },
    )

    result = chat.answer("take me to Jan 1 2020")

    assert "navigate" not in result
    assert "2020-01-01" in result["answer"]


def test_answer_view_brief_malformed_date_gives_clarifying_message(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [{"name": "view_brief", "arguments": {"date": "not-a-date"}}],
        },
    )

    result = chat.answer("take me to some day")

    assert "navigate" not in result
    assert "rephrase" in result["answer"]


def test_answer_view_brief_never_becomes_a_pending_action_even_when_alone(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The core guarantee: navigation must never require a confirm click,
    # unlike every other tool — regression-guards the interception
    # happening before the generic single-call pending_action branch.
    today = chat._today()
    queries.create_manual_item(today, "urgent", "Today task")
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [{"name": "view_brief", "arguments": {"date": today}}],
        },
    )

    result = chat.answer("go to today")

    assert "pending_action" not in result
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert rows == []  # navigation is never logged as a proposed/confirmed change


def test_answer_view_brief_takes_priority_over_a_mixed_call_in_the_same_turn(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = chat._today()
    queries.create_manual_item(today, "urgent", "Today task")
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [
                {"name": "create_task", "arguments": {"title": "X"}},
                {"name": "view_brief", "arguments": {"date": today}},
            ],
        },
    )

    result = chat.answer("show me today and also add a task called X")

    assert result["navigate"] == "/brief"
    assert "pending_action" not in result
    assert "pending_batch" not in result


def test_answer_returns_pending_action_for_a_single_move_task_to_date_call(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [
                {"name": "move_task_to_date", "arguments": {"item_id": 42, "target_date": "2026-08-17"}}
            ],
        },
    )

    result = chat.answer("bring that task to today")

    assert result["pending_action"]["tool"] == "move_task_to_date"
    assert result["pending_action"]["args"]["item_id"] == 42


def test_answer_returns_pending_batch_for_multiple_tool_calls_in_one_turn(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not a brain dump (single short request) — "bring all of yesterday's
    # tasks to today" naturally produces multiple move_task_to_date calls
    # in one model response, which must become a pending_batch just like
    # the brain-dump extraction path does, not just take tool_calls[0].
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [
                {"name": "move_task_to_date", "arguments": {"item_id": 1, "target_date": "2026-08-17"}},
                {"name": "move_task_to_date", "arguments": {"item_id": 2, "target_date": "2026-08-17"}},
            ],
        },
    )

    result = chat.answer("bring all of yesterday's tasks to today")

    assert "pending_batch" in result
    tasks = result["pending_batch"]["tasks"]
    assert len(tasks) == 2
    assert {t["args"]["item_id"] for t in tasks} == {1, 2}
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert len(rows) == 2
    assert all(r["status"] == "proposed" for r in rows)


def test_answer_multi_call_batch_skips_malformed_calls_but_keeps_valid_ones(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [
                {"name": "move_task_to_date", "arguments": {"item_id": 1, "target_date": "2026-08-17"}},
                {"name": "move_task_to_date", "arguments": {"target_date": "2026-08-17"}},  # missing item_id
            ],
        },
    )

    result = chat.answer("bring all of yesterday's tasks to today")

    assert "pending_batch" in result
    assert len(result["pending_batch"]["tasks"]) == 1
    assert result["pending_batch"]["tasks"][0]["args"]["item_id"] == 1


def test_answer_falls_back_to_plain_text_on_malformed_tool_call(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [{"name": "create_task", "arguments": None}],
        },
    )

    result = chat.answer("create a task")

    assert "pending_action" not in result
    assert "rephrase" in result["answer"]
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "proposed"


def test_confirm_action_cancel_does_not_call_dispatch(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = []
    monkeypatch.setattr(tools, "dispatch", lambda *a, **k: called.append(1))

    result = chat.confirm_action({"tool": "create_task", "args": {"title": "X"}}, confirmed=False)

    assert result["answer"] == "Okay, not making that change."
    assert called == []
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert rows[0]["status"] == "cancelled"


def test_confirm_action_confirmed_calls_dispatch_with_correct_args(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    def fake_dispatch(name, args, credentials):
        captured["name"] = name
        captured["args"] = args
        return {"id": "new-id"}

    monkeypatch.setattr(tools, "dispatch", fake_dispatch)
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")

    result = chat.confirm_action(
        {"tool": "create_task", "args": {"title": "Renew passport"}}, confirmed=True
    )

    assert captured["name"] == "create_task"
    assert captured["args"]["title"] == "Renew passport"
    assert "Done" in result["answer"]
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert rows[0]["status"] == "confirmed"


def test_confirm_action_move_task_to_date_never_requests_google_credentials(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise():
        raise AssertionError("move_task_to_date must not fetch Google credentials")

    monkeypatch.setattr(auth, "get_google_credentials", _raise)
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"moved": True})

    result = chat.confirm_action(
        {"tool": "move_task_to_date", "args": {"item_id": 1, "target_date": "2026-08-17"}},
        confirmed=True,
    )

    assert "Done" in result["answer"]


def test_confirm_action_move_task_to_date_resolves_title_from_history_before_moving(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date, timedelta

    yesterday = (date.fromisoformat(chat._today()) - timedelta(days=1)).isoformat()
    item_id = queries.create_manual_item(yesterday, "action_items", "Old task")

    result = chat.confirm_action(
        {"tool": "move_task_to_date", "args": {"item_id": item_id, "target_date": chat._today()}},
        confirmed=True,
    )

    assert "Old task" in result["answer"]
    with db.session() as conn:
        row = conn.execute("SELECT brief_date, status FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["brief_date"] == chat._today()
    assert row["status"] == "pending"


def test_confirm_action_move_task_to_date_failure_gives_local_error_message(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(name, args, credentials):
        raise RuntimeError("boom")

    monkeypatch.setattr(tools, "dispatch", _raise)

    result = chat.confirm_action(
        {"tool": "move_task_to_date", "args": {"item_id": 1, "target_date": "2026-08-17"}},
        confirmed=True,
    )

    assert "Google Tasks" not in result["answer"]
    assert "try again" in result["answer"]


def test_confirm_action_surfaces_auth_not_configured_message(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise():
        raise auth.AuthNotConfigured("No saved Google token — run `make auth` first.")

    monkeypatch.setattr(auth, "get_google_credentials", _raise)

    result = chat.confirm_action({"tool": "complete_task", "args": {"task_id": "t1"}}, confirmed=True)

    assert "make auth" in result["answer"]
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert rows[0]["status"] == "failed"


def test_update_item_from_task_updates_title(isolated_db: None) -> None:
    today = chat._today()
    with db.session() as conn:
        conn.execute(
            "INSERT INTO briefs (brief_date, generated_at, degraded_lanes) VALUES (?, ?, '[]')",
            (today, "2026-08-14T10:00:00"),
        )
        conn.execute(
            "INSERT INTO items (brief_date, lane, source, source_id, title, "
            "why_it_matters, suggested_next_step, priority, deep_link, status, created_at) "
            "VALUES (?, 'tasks_due', 'google_tasks', 'gtask-1', 'Old title', '', '', 2, '', 'pending', ?)",
            (today, "2026-08-14T10:00:00"),
        )

    updated = queries.update_item_from_task("gtask-1", title="New title")

    assert updated is True
    brief = queries.get_brief(today)
    assert brief["lanes"]["tasks_due"][0]["title"] == "New title"


def test_update_item_from_task_updates_status(isolated_db: None) -> None:
    today = chat._today()
    with db.session() as conn:
        conn.execute(
            "INSERT INTO briefs (brief_date, generated_at, degraded_lanes) VALUES (?, ?, '[]')",
            (today, "2026-08-14T10:00:00"),
        )
        conn.execute(
            "INSERT INTO items (brief_date, lane, source, source_id, title, "
            "why_it_matters, suggested_next_step, priority, deep_link, status, created_at) "
            "VALUES (?, 'tasks_due', 'google_tasks', 'gtask-2', 'Renew cert', '', '', 2, '', 'pending', ?)",
            (today, "2026-08-14T10:00:00"),
        )

    updated = queries.update_item_from_task("gtask-2", status="done")

    assert updated is True
    brief = queries.get_brief(today)
    assert brief["lanes"]["tasks_due"] == []  # get_brief only returns pending items


def test_update_item_from_task_no_op_without_title_or_status(isolated_db: None) -> None:
    assert queries.update_item_from_task("does-not-exist") is False


def test_update_item_from_task_returns_false_when_nothing_matches(isolated_db: None) -> None:
    assert queries.update_item_from_task("does-not-exist", title="X") is False


def test_confirm_action_create_task_inserts_local_item_immediately(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point of the fast path: no pipeline pull/retriage in
    # between — the local mirror row exists the moment confirm_action
    # returns, not after however long an LLM re-triage call takes.
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"id": "gtask-new"})

    chat.confirm_action(
        {"tool": "create_task", "args": {"title": "Renew passport"}}, confirmed=True
    )

    with db.session() as conn:
        row = conn.execute("SELECT * FROM items WHERE source_id = 'gtask-new'").fetchone()
    assert row is not None
    assert row["source"] == "google_tasks"
    assert row["title"] == "Renew passport"
    assert row["status"] == "pending"
    assert row["brief_date"] == chat._today()


def test_confirm_action_create_task_uses_stated_lane(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"id": "gtask-urgent"})

    chat.confirm_action(
        {"tool": "create_task", "args": {"title": "Renew passport", "lane": "urgent"}}, confirmed=True
    )

    with db.session() as conn:
        row = conn.execute("SELECT lane FROM items WHERE source_id = 'gtask-urgent'").fetchone()
    assert row["lane"] == "urgent"


def test_confirm_action_create_task_defaults_to_action_items_lane_when_unstated(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No triage pass runs anymore to auto-classify it, so an unstated
    # lane needs some neutral default rather than crashing/being blank.
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"id": "gtask-plain"})

    chat.confirm_action({"tool": "create_task", "args": {"title": "Just a task"}}, confirmed=True)

    with db.session() as conn:
        row = conn.execute("SELECT lane FROM items WHERE source_id = 'gtask-plain'").fetchone()
    assert row["lane"] == "action_items"


def test_confirm_action_create_task_applies_project_id(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = queries.create_registered_project("Daily Command Center", "/tmp/dcc")
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"id": "gtask-proj"})

    chat.confirm_action(
        {"tool": "create_task", "args": {"title": "Update README", "project_id": project_id}},
        confirmed=True,
    )

    with db.session() as conn:
        row = conn.execute("SELECT project_id FROM items WHERE source_id = 'gtask-proj'").fetchone()
    assert row["project_id"] == project_id


def test_confirm_action_create_task_sets_due_date(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"id": "gtask-due"})

    chat.confirm_action(
        {"tool": "create_task", "args": {"title": "Renew passport", "due_date": "2026-08-21"}},
        confirmed=True,
    )

    with db.session() as conn:
        row = conn.execute("SELECT due_date FROM items WHERE source_id = 'gtask-due'").fetchone()
    assert row["due_date"] == "2026-08-21"


def test_confirm_action_create_task_skips_local_insert_if_no_id_returned(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Google Tasks not returning an id would be unusual, but must not
    # crash or surface an error — the real creation already succeeded.
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {})

    result = chat.confirm_action(
        {"tool": "create_task", "args": {"title": "X", "lane": "urgent"}}, confirmed=True
    )

    assert "Done" in result["answer"]
    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    assert count == 0


def test_confirm_action_update_task_syncs_title_directly_without_a_pull(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"id": args["task_id"]})
    monkeypatch.setattr(
        pipeline, "run_source", lambda name: (_ for _ in ()).throw(AssertionError("no pull expected"))
    )
    captured = {}
    monkeypatch.setattr(
        queries,
        "update_item_from_task",
        lambda source_id, title=None, status=None: captured.update(
            source_id=source_id, title=title, status=status
        ),
    )

    chat.confirm_action(
        {"tool": "update_task", "args": {"task_id": "t1", "title": "New title"}}, confirmed=True
    )

    assert captured == {"source_id": "t1", "title": "New title", "status": None}


def test_confirm_action_complete_task_syncs_status_directly(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"id": args["task_id"]})
    captured = {}
    monkeypatch.setattr(
        queries,
        "update_item_from_task",
        lambda source_id, title=None, status=None: captured.update(
            source_id=source_id, title=title, status=status
        ),
    )

    chat.confirm_action({"tool": "complete_task", "args": {"task_id": "t1"}}, confirmed=True)

    assert captured == {"source_id": "t1", "title": None, "status": "done"}


def test_confirm_action_complete_task_resolves_title_before_marking_done(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression test: the local-state sync flips the item to
    # status='done', which drops it from get_brief()'s pending-only
    # results. describe_done must resolve the title from a lookup taken
    # BEFORE that sync runs, or it falls back to showing the raw task id.
    today = chat._today()
    with db.session() as conn:
        conn.execute(
            "INSERT INTO briefs (brief_date, generated_at, degraded_lanes) VALUES (?, ?, '[]')",
            (today, "2026-08-14T10:00:00"),
        )
        conn.execute(
            "INSERT INTO items (brief_date, lane, source, source_id, title, "
            "why_it_matters, suggested_next_step, priority, deep_link, status, created_at) "
            "VALUES (?, 'tasks_due', 'google_tasks', 'gtask-9', 'Renew passport', '', '', 2, '', 'pending', ?)",
            (today, "2026-08-14T10:00:00"),
        )
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"id": args["task_id"]})

    result = chat.confirm_action({"tool": "complete_task", "args": {"task_id": "gtask-9"}}, confirmed=True)

    assert "Renew passport" in result["answer"]
    assert "gtask-9" not in result["answer"]


def test_confirm_action_still_succeeds_if_local_sync_fails(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"id": "new-id"})

    def _raise(**kwargs):
        raise RuntimeError("sync boom")

    monkeypatch.setattr(queries, "create_synced_task_item", _raise)

    result = chat.confirm_action({"tool": "create_task", "args": {"title": "X"}}, confirmed=True)

    # The real Google Tasks change already succeeded — a local sync
    # failure must not turn that into an error message.
    assert "Done" in result["answer"]


def test_answer_includes_prior_conversation_turns(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    captured = {}

    def _fake(messages, tools):
        captured["messages"] = messages
        return {"content": "August 21st.", "tool_calls": []}

    monkeypatch.setattr(triage, "run_groq_chat_with_tools", _fake)

    history = [
        {"role": "user", "content": "create a task to renew my license"},
        {"role": "assistant", "content": "When is this due?"},
    ]
    chat.answer("next Friday", history=history)

    messages = captured["messages"]
    # system, then the two history turns, then the current question.
    assert messages[1] == {"role": "user", "content": "create a task to renew my license"}
    assert messages[2] == {"role": "assistant", "content": "When is this due?"}
    assert "next Friday" in messages[3]["content"]


def test_confirm_action_wraps_dispatch_failure_as_plain_text(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")

    def _raise(*a, **k):
        raise RuntimeError("Google API is down")

    monkeypatch.setattr(tools, "dispatch", _raise)

    result = chat.confirm_action({"tool": "create_task", "args": {"title": "X"}}, confirmed=True)

    assert "Couldn't reach Google Tasks" in result["answer"]
    assert "Google API is down" not in result["answer"]  # not a raw exception
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert rows[0]["status"] == "failed"


# --- _looks_like_brain_dump -------------------------------------------------


def test_looks_like_brain_dump_three_or_more_lines() -> None:
    assert chat._looks_like_brain_dump("Buy milk\nCall the dentist\nFinish the report") is True


def test_looks_like_brain_dump_two_lines_is_not_a_dump() -> None:
    assert chat._looks_like_brain_dump("Buy milk\nCall the dentist") is False


def test_looks_like_brain_dump_short_question_is_not_a_dump() -> None:
    assert chat._looks_like_brain_dump("What's due today?") is False


def test_looks_like_brain_dump_three_long_sentences() -> None:
    text = (
        "I need to renew my passport before it expires next month. "
        "Also I should call the dentist about that appointment. "
        "And don't forget to finish the quarterly report by Friday."
    )
    assert chat._looks_like_brain_dump(text) is True


def test_looks_like_brain_dump_three_short_clauses_under_length_floor() -> None:
    assert chat._looks_like_brain_dump("Done. Done. Done.") is False


def test_looks_like_brain_dump_single_run_on_sentence_is_not_a_dump() -> None:
    text = (
        "so I was thinking about maybe eventually getting around to possibly "
        "reorganizing the garage at some point this year or maybe next"
    )
    assert chat._looks_like_brain_dump(text) is False


def test_looks_like_brain_dump_bulleted_list() -> None:
    text = "- renew passport\n- call dentist\n- finish report"
    assert chat._looks_like_brain_dump(text) is True


# --- _extract_tasks ----------------------------------------------------------


def test_extract_tasks_well_formed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [
                {
                    "name": "extract_tasks",
                    "arguments": {
                        "tasks": [
                            {"title": "Renew passport", "due_date": "2026-08-21"},
                            {"title": "Call dentist", "notes": "ask about cleaning"},
                        ]
                    },
                }
            ],
        },
    )

    result = chat._extract_tasks("dump text")

    assert result == [
        {"title": "Renew passport", "due_date": "2026-08-21", "notes": None},
        {"title": "Call dentist", "due_date": None, "notes": "ask about cleaning"},
    ]


def test_extract_tasks_no_tool_calls_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        triage, "run_groq_chat_with_tools", lambda messages, tools: {"content": "no tasks here", "tool_calls": []}
    )
    assert chat._extract_tasks("just chatting") == []


def test_extract_tasks_malformed_arguments_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [{"name": "extract_tasks", "arguments": None}],
        },
    )
    assert chat._extract_tasks("dump text") == []


def test_extract_tasks_wrong_tool_name_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [{"name": "create_task", "arguments": {"title": "X"}}],
        },
    )
    assert chat._extract_tasks("dump text") == []


def test_extract_tasks_missing_tasks_key_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [{"name": "extract_tasks", "arguments": {}}],
        },
    )
    assert chat._extract_tasks("dump text") == []


def test_extract_tasks_drops_blank_title_items(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        triage,
        "run_groq_chat_with_tools",
        lambda messages, tools: {
            "content": None,
            "tool_calls": [
                {
                    "name": "extract_tasks",
                    "arguments": {"tasks": [{"title": "  "}, {"title": "Real task"}]},
                }
            ],
        },
    )

    result = chat._extract_tasks("dump text")

    assert result == [{"title": "Real task", "due_date": None, "notes": None}]


# --- answer()'s brain-dump branch --------------------------------------------

_DUMP_TEXT = "Renew my passport by Friday.\nCall the dentist about that thing.\nFinish the quarterly report."


def test_answer_returns_pending_batch_for_multiple_extracted_tasks(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        chat,
        "_extract_tasks",
        lambda question: [
            {"title": "Renew passport", "due_date": "2026-08-21", "notes": None},
            {"title": "Call dentist", "due_date": None, "notes": None},
        ],
    )

    result = chat.answer(_DUMP_TEXT)

    assert "pending_batch" in result
    batch_tasks = result["pending_batch"]["tasks"]
    assert len(batch_tasks) == 2
    assert batch_tasks[0]["tool"] == "create_task"
    assert "Renew passport" in batch_tasks[0]["confirmation_text"]
    assert "Call dentist" in batch_tasks[1]["confirmation_text"]

    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert len(rows) == 2
    assert all(r["status"] == "proposed" for r in rows)


def test_answer_returns_single_pending_action_for_one_extracted_task(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        chat, "_extract_tasks", lambda question: [{"title": "Renew passport", "due_date": None, "notes": None}]
    )

    result = chat.answer(_DUMP_TEXT)

    assert "pending_action" in result
    assert "pending_batch" not in result
    assert result["pending_action"]["tool"] == "create_task"
    assert result["pending_action"]["args"]["title"] == "Renew passport"

    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "proposed"


def test_answer_falls_through_to_normal_flow_when_extraction_finds_nothing(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    calls = []

    def _fake(messages, tools):
        calls.append(tools)
        if tools is chat.tools.EXTRACT_TASKS_SCHEMA:
            return {"content": None, "tool_calls": [{"name": "extract_tasks", "arguments": {"tasks": []}}]}
        return {"content": "Nothing actionable found — how can I help?", "tool_calls": []}

    monkeypatch.setattr(triage, "run_groq_chat_with_tools", _fake)

    result = chat.answer(_DUMP_TEXT)

    assert len(calls) == 2  # extraction call, then the normal fallback call
    assert result["answer"] == "Nothing actionable found — how can I help?"


def test_answer_does_not_attempt_extraction_for_a_normal_question(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    extract_called = []
    monkeypatch.setattr(chat, "_extract_tasks", lambda q: extract_called.append(q) or [])
    monkeypatch.setattr(triage, "run_groq_chat_with_tools", _no_tool_calls("Here's your answer."))

    chat.answer("What's due today?")

    assert extract_called == []


def test_answer_does_not_attempt_extraction_when_history_present(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval, "top_k", lambda query, k=5: [])
    extract_called = []
    monkeypatch.setattr(chat, "_extract_tasks", lambda q: extract_called.append(q) or [])
    monkeypatch.setattr(triage, "run_groq_chat_with_tools", _no_tool_calls("answer"))

    history = [
        {"role": "assistant", "content": "What's the due date and any details?"},
    ]
    chat.answer(_DUMP_TEXT, history=history)

    assert extract_called == []


# --- confirm_batch -----------------------------------------------------------


def test_confirm_batch_all_checked_all_succeed(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = iter(["new-id-a", "new-id-b"])
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"id": next(ids)})

    tasks = [
        {"tool": "create_task", "args": {"title": "A"}, "checked": True},
        {"tool": "create_task", "args": {"title": "B"}, "checked": True},
    ]
    result = chat.confirm_batch(tasks, confirmed=True)

    assert "A" in result["answer"] and "B" in result["answer"]
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log ORDER BY id").fetchall()
        item_titles = {r["title"] for r in conn.execute("SELECT title FROM items").fetchall()}
    assert len(rows) == 2
    assert all(r["status"] == "confirmed" for r in rows)
    assert item_titles == {"A", "B"}  # both landed locally immediately, no pull needed


def test_confirm_batch_partial_checked(isolated_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    dispatched = []
    monkeypatch.setattr(
        tools, "dispatch", lambda name, args, credentials: dispatched.append(args["title"]) or {"id": "x"}
    )

    tasks = [
        {"tool": "create_task", "args": {"title": "A"}, "checked": True},
        {"tool": "create_task", "args": {"title": "B"}, "checked": False},
    ]
    chat.confirm_batch(tasks, confirmed=True)

    assert dispatched == ["A"]
    with db.session() as conn:
        rows = conn.execute("SELECT args_json, status FROM tool_call_log ORDER BY id").fetchall()
    statuses = {r["args_json"]: r["status"] for r in rows}
    assert statuses['{"title": "A"}'] == "confirmed"
    assert statuses['{"title": "B"}'] == "cancelled"


def test_confirm_batch_all_unchecked_but_confirmed_true_is_a_safe_no_op(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_called = []
    monkeypatch.setattr(auth, "get_google_credentials", lambda: auth_called.append(1) or "fake-creds")
    dispatch_called = []
    monkeypatch.setattr(tools, "dispatch", lambda *a, **k: dispatch_called.append(1))

    tasks = [
        {"tool": "create_task", "args": {"title": "A"}, "checked": False},
        {"tool": "create_task", "args": {"title": "B"}, "checked": False},
    ]
    result = chat.confirm_batch(tasks, confirmed=True)

    assert result["answer"] == "Okay, not creating those tasks."
    assert auth_called == []
    assert dispatch_called == []
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert len(rows) == 2
    assert all(r["status"] == "cancelled" for r in rows)


def test_confirm_batch_whole_batch_cancel(isolated_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch_called = []
    monkeypatch.setattr(tools, "dispatch", lambda *a, **k: dispatch_called.append(1))

    tasks = [
        {"tool": "create_task", "args": {"title": "A"}, "checked": True},
        {"tool": "create_task", "args": {"title": "B"}, "checked": False},
    ]
    result = chat.confirm_batch(tasks, confirmed=False)

    assert result["answer"] == "Okay, not creating those tasks."
    assert dispatch_called == []
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert len(rows) == 2
    assert all(r["status"] == "cancelled" for r in rows)


def test_confirm_batch_one_dispatch_failure_among_several(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")

    def fake_dispatch(name, args, credentials):
        if args["title"] == "Bad task":
            raise RuntimeError("boom")
        return {"id": "ok"}

    monkeypatch.setattr(tools, "dispatch", fake_dispatch)

    tasks = [
        {"tool": "create_task", "args": {"title": "Good task"}, "checked": True},
        {"tool": "create_task", "args": {"title": "Bad task"}, "checked": True},
    ]
    result = chat.confirm_batch(tasks, confirmed=True)

    assert "Good task" in result["answer"]
    assert "Bad task" in result["answer"]
    with db.session() as conn:
        rows = conn.execute("SELECT args_json, status FROM tool_call_log ORDER BY id").fetchall()
    statuses = {r["args_json"]: r["status"] for r in rows}
    assert statuses['{"title": "Good task"}'] == "confirmed"
    assert statuses['{"title": "Bad task"}'] == "failed"


def test_confirm_batch_pure_move_batch_never_requests_google_credentials(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise():
        raise AssertionError("a move-only batch must not fetch Google credentials")

    monkeypatch.setattr(auth, "get_google_credentials", _raise)
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"moved": True})

    tasks = [
        {"tool": "move_task_to_date", "args": {"item_id": 1, "target_date": "2026-08-17"}, "checked": True},
        {"tool": "move_task_to_date", "args": {"item_id": 2, "target_date": "2026-08-17"}, "checked": True},
    ]
    result = chat.confirm_batch(tasks, confirmed=True)

    assert "Done" in result["answer"]
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert all(r["status"] == "confirmed" for r in rows)


def test_confirm_batch_mixed_create_and_move_batch(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")
    monkeypatch.setattr(tools, "dispatch", lambda name, args, credentials: {"id": "new-id", "moved": True})

    tasks = [
        {"tool": "create_task", "args": {"title": "New task"}, "checked": True},
        {"tool": "move_task_to_date", "args": {"item_id": 1, "target_date": "2026-08-17"}, "checked": True},
    ]
    result = chat.confirm_batch(tasks, confirmed=True)

    assert "New task" in result["answer"]  # create_task's existing "Created N tasks" wording preserved
    assert "Done" in result["answer"]  # move_task_to_date's own describe_done sentence included too
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log ORDER BY id").fetchall()
    assert len(rows) == 2
    assert all(r["status"] == "confirmed" for r in rows)


def test_confirm_batch_move_dispatch_failure_reports_as_couldnt_make_changes(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(name, args, credentials):
        raise RuntimeError("boom")

    monkeypatch.setattr(tools, "dispatch", _raise)

    tasks = [
        {"tool": "move_task_to_date", "args": {"item_id": 1, "target_date": "2026-08-17"}, "checked": True}
    ]
    result = chat.confirm_batch(tasks, confirmed=True)

    assert "Couldn't make" in result["answer"]
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM tool_call_log").fetchall()
    assert rows[0]["status"] == "failed"


def test_confirm_batch_auth_not_configured(isolated_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise():
        raise auth.AuthNotConfigured("No saved Google token — run `make auth` first.")

    monkeypatch.setattr(auth, "get_google_credentials", _raise)

    tasks = [
        {"tool": "create_task", "args": {"title": "A"}, "checked": True},
        {"tool": "create_task", "args": {"title": "B"}, "checked": False},
    ]
    result = chat.confirm_batch(tasks, confirmed=True)

    assert "make auth" in result["answer"]
    with db.session() as conn:
        rows = conn.execute("SELECT args_json, status FROM tool_call_log ORDER BY id").fetchall()
    statuses = {r["args_json"]: r["status"] for r in rows}
    assert statuses['{"title": "A"}'] == "failed"
    assert statuses['{"title": "B"}'] == "cancelled"
