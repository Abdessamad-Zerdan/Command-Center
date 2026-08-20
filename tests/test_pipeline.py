import json
from datetime import datetime
from pathlib import Path

import pytest

from command_center import db, pipeline, queries
from command_center.config import TZ
from command_center.sources import RawItem


class _FakeGmailOK:
    def __init__(self, credentials) -> None:
        pass

    def fetch(self) -> list[RawItem]:
        return [
            RawItem(
                source="gmail",
                source_id="m1",
                title="Test subject",
                body="Body text",
                metadata={"deep_link": "https://mail.google.com/x"},
            )
        ]


class _FakeGmailFails:
    def __init__(self, credentials) -> None:
        pass

    def fetch(self) -> list[RawItem]:
        raise RuntimeError("simulated Gmail API failure")


class _FakeCalendarEmpty:
    def __init__(self, credentials) -> None:
        pass

    def fetch_with_events(self) -> tuple[list[RawItem], list[dict]]:
        return [], []


class _FakeTasksEmpty:
    def __init__(self, credentials) -> None:
        pass

    def fetch(self) -> list[RawItem]:
        return []


class _FakeTasksFails:
    def __init__(self, credentials) -> None:
        pass

    def fetch(self) -> list[RawItem]:
        raise RuntimeError("simulated Tasks API failure")


def _fake_triage_run(raw_items: list[RawItem]) -> list[dict]:
    return [
        {
            "lane": "action_items",
            "source": ri.source,
            "source_id": ri.source_id,
            "title": ri.title,
            "why_it_matters": "matters",
            "suggested_next_step": "do it",
            "priority": 2,
            "deep_link": ri.metadata.get("deep_link", ""),
        }
        for ri in raw_items
    ]


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def test_pipeline_saves_triaged_items(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "get_google_credentials", lambda: object())
    monkeypatch.setattr(pipeline, "GmailSource", _FakeGmailOK)
    monkeypatch.setattr(pipeline, "CalendarSource", _FakeCalendarEmpty)
    monkeypatch.setattr(pipeline, "TasksSource", _FakeTasksEmpty)
    monkeypatch.setattr(pipeline.medium, "fetch_and_rank", lambda: [])
    monkeypatch.setattr(pipeline.triage, "run", _fake_triage_run)

    pipeline.run()

    brief = queries.get_brief(_today())
    assert brief is not None
    assert brief["degraded_lanes"] == []
    titles = [item["title"] for item in brief["lanes"]["action_items"]]
    assert "Test subject" in titles


def test_pipeline_applies_triage_rules_after_triage(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries.create_triage_rule("title", "test subject", "urgent")
    monkeypatch.setattr(pipeline, "get_google_credentials", lambda: object())
    monkeypatch.setattr(pipeline, "GmailSource", _FakeGmailOK)
    monkeypatch.setattr(pipeline, "CalendarSource", _FakeCalendarEmpty)
    monkeypatch.setattr(pipeline, "TasksSource", _FakeTasksEmpty)
    monkeypatch.setattr(pipeline.medium, "fetch_and_rank", lambda: [])
    # _fake_triage_run always classifies into action_items — the rule
    # above overrides that to urgent, proving apply_rules runs after it.
    monkeypatch.setattr(pipeline.triage, "run", _fake_triage_run)

    pipeline.run()

    brief = queries.get_brief(_today())
    urgent_titles = [item["title"] for item in brief["lanes"]["urgent"]]
    action_titles = [item["title"] for item in brief["lanes"]["action_items"]]
    assert "Test subject" in urgent_titles
    assert "Test subject" not in action_titles


def test_pipeline_a_broken_rule_does_not_discard_a_successful_triage(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "get_google_credentials", lambda: object())
    monkeypatch.setattr(pipeline, "GmailSource", _FakeGmailOK)
    monkeypatch.setattr(pipeline, "CalendarSource", _FakeCalendarEmpty)
    monkeypatch.setattr(pipeline, "TasksSource", _FakeTasksEmpty)
    monkeypatch.setattr(pipeline.medium, "fetch_and_rank", lambda: [])
    monkeypatch.setattr(pipeline.triage, "run", _fake_triage_run)

    def _raise(items, raw_items):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline.triage_rules, "apply_rules", _raise)

    pipeline.run()

    brief = queries.get_brief(_today())
    assert brief["degraded_lanes"] == []  # a rules bug must not degrade an already-good triage
    titles = [item["title"] for item in brief["lanes"]["action_items"]]
    assert "Test subject" in titles


def test_pipeline_degrades_on_source_failure(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "get_google_credentials", lambda: object())
    monkeypatch.setattr(pipeline, "GmailSource", _FakeGmailFails)
    monkeypatch.setattr(pipeline, "CalendarSource", _FakeCalendarEmpty)
    monkeypatch.setattr(pipeline, "TasksSource", _FakeTasksEmpty)
    monkeypatch.setattr(pipeline.medium, "fetch_and_rank", lambda: [])
    monkeypatch.setattr(pipeline.triage, "run", _fake_triage_run)

    pipeline.run()

    brief = queries.get_brief(_today())
    assert brief is not None
    assert "gmail" in brief["degraded_lanes"]


def test_pipeline_force_resurfaces_already_triaged_item(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "get_google_credentials", lambda: object())
    monkeypatch.setattr(pipeline, "GmailSource", _FakeGmailOK)
    monkeypatch.setattr(pipeline, "CalendarSource", _FakeCalendarEmpty)
    monkeypatch.setattr(pipeline, "TasksSource", _FakeTasksEmpty)
    monkeypatch.setattr(pipeline.medium, "fetch_and_rank", lambda: [])
    monkeypatch.setattr(pipeline.triage, "run", _fake_triage_run)

    pipeline.run()
    with db.session() as conn:
        conn.execute("UPDATE items SET status = 'done' WHERE source_id = 'm1'")

    pipeline.run()  # non-force: dedup keeps it out
    brief = queries.get_brief(_today())
    assert brief["lanes"]["action_items"] == []

    pipeline.run(force=True)  # force: resurfaces as pending again
    brief = queries.get_brief(_today())
    titles = [item["title"] for item in brief["lanes"]["action_items"]]
    assert "Test subject" in titles


def test_dismissed_item_never_resurfaces_on_a_non_force_pull(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same guarantee as the 'done' case above: dismiss uses the same
    # generic set_item_status() + (source, source_id) INSERT OR IGNORE
    # dedup, so a dismissed item's row is never re-inserted as pending.
    monkeypatch.setattr(pipeline, "get_google_credentials", lambda: object())
    monkeypatch.setattr(pipeline, "GmailSource", _FakeGmailOK)
    monkeypatch.setattr(pipeline, "CalendarSource", _FakeCalendarEmpty)
    monkeypatch.setattr(pipeline, "TasksSource", _FakeTasksEmpty)
    monkeypatch.setattr(pipeline.medium, "fetch_and_rank", lambda: [])
    monkeypatch.setattr(pipeline.triage, "run", _fake_triage_run)

    pipeline.run()
    with db.session() as conn:
        item_id = conn.execute(
            "SELECT id FROM items WHERE source_id = 'm1'"
        ).fetchone()["id"]
    queries.set_item_status(item_id, "dismissed")

    pipeline.run()  # non-force: dedup keeps it out
    brief = queries.get_brief(_today())
    assert brief["lanes"]["action_items"] == []

    with db.session() as conn:
        status = conn.execute(
            "SELECT status FROM items WHERE id = ?", (item_id,)
        ).fetchone()["status"]
    assert status == "dismissed"


def test_run_source_merges_degraded_lanes_and_preserves_calendar(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "get_google_credentials", lambda: object())
    monkeypatch.setattr(pipeline, "GmailSource", _FakeGmailOK)
    monkeypatch.setattr(pipeline.triage, "run", _fake_triage_run)

    with db.session() as conn:
        conn.execute(
            "INSERT INTO briefs (brief_date, generated_at, degraded_lanes) VALUES (?, ?, ?)",
            (_today(), "2026-08-14T09:00:00+03:00", json.dumps(["tasks", "medium"])),
        )
        conn.execute(
            "INSERT INTO calendar_events "
            "(brief_date, title, start_time, end_time, attendees, link) "
            "VALUES (?, 'Standup', '2026-08-14T09:30:00+03:00', "
            "'2026-08-14T10:00:00+03:00', '[]', NULL)",
            (_today(),),
        )

    pipeline.run_source("gmail")

    brief = queries.get_brief(_today())
    # gmail succeeded so it's not degraded; tasks/medium weren't attempted
    # this round, so their prior degraded status must survive untouched.
    assert brief["degraded_lanes"] == ["tasks", "medium"]
    # A gmail-only pull must not wipe today's calendar timeline strip.
    assert len(brief["events"]) == 1
    assert brief["events"][0]["title"] == "Standup"
    titles = [item["title"] for item in brief["lanes"]["action_items"]]
    assert "Test subject" in titles


def test_run_source_tasks_failure_degrades_only_tasks(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "get_google_credentials", lambda: object())
    monkeypatch.setattr(pipeline, "TasksSource", _FakeTasksFails)

    pipeline.run_source("tasks")

    brief = queries.get_brief(_today())
    assert brief is not None
    assert brief["degraded_lanes"] == ["tasks"]


def test_run_source_unknown_name_raises(isolated_db: None) -> None:
    with pytest.raises(ValueError, match="Unknown source"):
        pipeline.run_source("carrier-pigeon")


def test_run_source_medium_uses_fetch_and_rank(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pipeline.medium,
        "fetch_and_rank",
        lambda: [
            {
                "lane": "reading",
                "source": "medium",
                "source_id": "a1",
                "title": "An article",
                "why_it_matters": "relevant",
                "suggested_next_step": "Read it",
                "priority": 2,
                "deep_link": "https://medium.com/a1",
            }
        ],
    )

    pipeline.run_source("medium")

    brief = queries.get_brief(_today())
    titles = [item["title"] for item in brief["lanes"]["reading"]]
    assert "An article" in titles


def test_run_source_medium_failure_degrades_only_reading(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fails():
        raise RuntimeError("simulated Medium API failure")

    monkeypatch.setattr(pipeline.medium, "fetch_and_rank", _fails)

    pipeline.run_source("medium")

    brief = queries.get_brief(_today())
    assert brief is not None
    assert "medium" in brief["degraded_lanes"]


def test_seed_source_config_sets_last_pulled_at_not_null(isolated_db: None) -> None:
    pipeline.seed_source_config()
    configs = {c["source_name"]: c for c in queries.list_source_configs()}
    assert set(configs) == {"gmail", "calendar", "tasks", "medium"}
    for cfg in configs.values():
        assert cfg["last_pulled_at"] is not None
        assert cfg["enabled"] == 1


def test_seed_source_config_uses_per_source_default_intervals(isolated_db: None) -> None:
    # gmail/calendar/tasks are action-oriented (4h); medium is pure
    # content discovery with no same-day urgency (daily) — see the audit
    # that motivated DEFAULT_SOURCE_INTERVALS.
    pipeline.seed_source_config()
    configs = {c["source_name"]: c["interval_minutes"] for c in queries.list_source_configs()}

    assert configs["gmail"] == 240
    assert configs["calendar"] == 240
    assert configs["tasks"] == 240
    assert configs["medium"] == 1440
