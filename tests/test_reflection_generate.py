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


def test_generate_reflection_propagates_provider_error_uncaught(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(messages, max_tokens=1024):
        raise TriageProviderError("boom")

    monkeypatch.setattr(triage, "run_groq_chat", _raise)

    with pytest.raises(TriageProviderError):
        reflection.generate_reflection("week", date(2026, 8, 10), date(2026, 8, 17))
