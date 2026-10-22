import json
from datetime import date
from pathlib import Path

import pytest

from command_center import db, triage
from command_center.history import reflection


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


@pytest.fixture()
def counting_groq(monkeypatch: pytest.MonkeyPatch) -> list:
    calls = []

    def _fake(messages, max_tokens=1024):
        calls.append(messages)
        return f"Reflection #{len(calls)}"

    monkeypatch.setattr(triage, "run_groq_chat", _fake)
    return calls


def _add_event(task_id: int, event_type: str, timestamp: str) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, event_type, timestamp, metadata_json) VALUES (?, ?, ?, ?)",
            (task_id, event_type, timestamp, json.dumps({"lane": "urgent", "source": "gmail", "title": "T"})),
        )


def test_day_period_raises_without_touching_db_or_groq(isolated_db: None, counting_groq: list) -> None:
    with pytest.raises(ValueError):
        reflection.get_or_generate_reflection("day", date(2026, 8, 14), date(2026, 8, 15))

    assert counting_groq == []
    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM reflection_cache").fetchone()["n"]
    assert count == 0
