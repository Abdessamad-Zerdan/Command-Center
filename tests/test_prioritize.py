"""prioritize.py: one model call sorts today's open tasks into High /
Medium / Low with a reason — only when the task list changed, never
touching a priority the user set, never trusting ids it didn't send."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from command_center import db, pipeline, prioritize, queries, triage

_REAL_RUN = prioritize.run  # conftest stubs it for every other test
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
TODAY = "2026-10-22"


@pytest.fixture(autouse=True)
def _real_prioritize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setattr(prioritize, "run", _REAL_RUN)
    monkeypatch.setattr(queries, "get_summary_language", lambda: "English")


@pytest.fixture()
def model(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[dict]] = []
    reply: dict = {"tasks": []}

    def fake(messages, max_tokens=1024):
        calls.append(messages)
        return reply if isinstance(reply, str) else json.dumps(reply)

    monkeypatch.setattr(triage, "run_groq_chat", fake)
    return calls, reply


def _task(title: str, lane: str = "urgent", due: str | None = None, brief_date: str = TODAY) -> int:
    return queries.create_manual_item(brief_date, lane, title, due)


def _priority(item_id: int) -> tuple[int, str | None]:
    with db.session() as conn:
        row = conn.execute("SELECT priority, priority_reason FROM items WHERE id = ?", (item_id,)).fetchone()
    return row["priority"], row["priority_reason"]


def test_a_bad_reply_raises_and_leaves_priorities_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task("Real")
    monkeypatch.setattr(triage, "run_groq_chat", lambda messages, max_tokens=1024: "not json")

    with pytest.raises(triage.TriageProviderError):
        prioritize.run(TODAY, NOW)

    assert _priority(task) == (2, None)
    # Not recorded as judged: the next pull tries again.
    assert queries.get_app_value("prioritize_fingerprint") is None


def test_a_failed_run_is_recorded_for_the_brief_and_cleared_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*args, **kwargs):
        raise triage.TriageProviderError("Groq unreachable")

    monkeypatch.setattr(prioritize, "run", down)
    pipeline._prioritize()
    assert queries.get_app_value("prioritize_failed_at")

    monkeypatch.setattr(prioritize, "run", lambda *a, **k: 0)
    pipeline._prioritize()
    assert queries.get_app_value("prioritize_failed_at") == ""


# --- the user's own priority changes lock it -------------------------------------------


def test_dragging_into_another_priority_band_locks_it(model) -> None:
    high = _task("High one")
    low = _task("Low one")
    queries.save_model_priorities([(high, 1, "a"), (low, 3, "b")])

    queries.reorder_item(low, after_item_id=high)  # dropped among the High ones

    with db.session() as conn:
        row = conn.execute("SELECT priority, priority_locked FROM items WHERE id = ?", (low,)).fetchone()
    assert (row["priority"], row["priority_locked"]) == (1, 1)


def test_reordering_within_the_same_band_does_not_lock(model) -> None:
    a = _task("A")
    b = _task("B")
    queries.reorder_item(b, after_item_id=None)  # to the top, still Medium like A

    with db.session() as conn:
        rows = conn.execute("SELECT priority_locked FROM items WHERE id IN (?, ?)", (a, b)).fetchall()
    assert [r["priority_locked"] for r in rows] == [0, 0]
