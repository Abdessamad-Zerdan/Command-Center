from datetime import date
from pathlib import Path

import pytest

from command_center import db, triage
from command_center.history import reflection
from command_center.triage import TriageProviderError


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_generate_reflection_raises_for_day_period(isolated_db: None) -> None:
    with pytest.raises(ValueError):
        reflection.generate_reflection("day", date(2026, 8, 14), date(2026, 8, 15))


def test_generate_reflection_calls_run_groq_chat_with_system_and_user_messages(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    def _fake(messages, max_tokens=1024):
        captured["messages"] = messages
        captured["max_tokens"] = max_tokens
        return "  A short reflection.  "

    monkeypatch.setattr(triage, "run_groq_chat", _fake)

    result = reflection.generate_reflection("week", date(2026, 8, 10), date(2026, 8, 17))

    assert result == "A short reflection."  # stripped
    messages = captured["messages"]
    assert messages[0] == {"role": "system", "content": reflection.SYSTEM_PROMPT}
    assert messages[1]["role"] == "user"
    assert "Period: week" in messages[1]["content"]
    assert captured["max_tokens"] == 300


def test_generate_reflection_propagates_provider_error_uncaught(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(messages, max_tokens=1024):
        raise TriageProviderError("boom")

    monkeypatch.setattr(triage, "run_groq_chat", _raise)

    with pytest.raises(TriageProviderError):
        reflection.generate_reflection("week", date(2026, 8, 10), date(2026, 8, 17))
