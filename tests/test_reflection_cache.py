import json
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


def test_cache_miss_calls_groq_once_and_stores_row(isolated_db: None, counting_groq: list) -> None:
    start, end = date(2026, 8, 10), date(2026, 8, 17)

    text = reflection.get_or_generate_reflection("week", start, end)

    assert text == "Reflection #1"
    assert len(counting_groq) == 1
    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM reflection_cache WHERE period_type='week' AND period_start=?", (start.isoformat(),)
        ).fetchone()
    assert row["generated_text"] == "Reflection #1"
    assert row["event_count"] == 0


def test_cache_hit_does_not_call_groq_again(isolated_db: None, counting_groq: list) -> None:
    start, end = date(2026, 8, 10), date(2026, 8, 17)

    first = reflection.get_or_generate_reflection("week", start, end)
    second = reflection.get_or_generate_reflection("week", start, end)

    assert second == first
    assert len(counting_groq) == 1


def test_new_event_before_end_invalidates_cache(isolated_db: None, counting_groq: list) -> None:
    start, end = date(2026, 8, 10), date(2026, 8, 17)

    reflection.get_or_generate_reflection("week", start, end)
    _add_event(1, "created", "2026-08-12T09:00:00+03:00")  # inside the period, < end
    second = reflection.get_or_generate_reflection("week", start, end)

    assert len(counting_groq) == 2
    assert second == "Reflection #2"
    with db.session() as conn:
        row = conn.execute(
            "SELECT event_count FROM reflection_cache WHERE period_type='week' AND period_start=?",
            (start.isoformat(),),
        ).fetchone()
    assert row["event_count"] == 1


def test_force_regenerates_even_when_event_count_unchanged(isolated_db: None, counting_groq: list) -> None:
    start, end = date(2026, 8, 10), date(2026, 8, 17)

    reflection.get_or_generate_reflection("week", start, end)
    second = reflection.get_or_generate_reflection("week", start, end, force=True)

    assert len(counting_groq) == 2
    assert second == "Reflection #2"


def test_elapsed_period_cache_survives_activity_that_lands_after_its_own_end(
    isolated_db: None, counting_groq: list
) -> None:
    # A fully-past period: [start, end) entirely before "now" isn't needed
    # here — the invalidation query is a literal timestamp < end.isoformat()
    # comparison, not a real-clock check, so seeding an event with a
    # timestamp AT/AFTER `end` exercises the exact same guarantee a truly
    # elapsed period gets in production: that row can never affect this
    # window's event_count.
    start, end = date(2026, 8, 10), date(2026, 8, 17)

    reflection.get_or_generate_reflection("week", start, end)
    _add_event(1, "created", "2026-08-17T00:00:00+03:00")  # exactly at end — excluded
    _add_event(1, "completed", "2026-09-01T00:00:00+03:00")  # well after end
    second = reflection.get_or_generate_reflection("week", start, end)

    assert len(counting_groq) == 1  # no second Groq call — cache still valid
    assert second == "Reflection #1"


def test_day_period_raises_without_touching_db_or_groq(isolated_db: None, counting_groq: list) -> None:
    with pytest.raises(ValueError):
        reflection.get_or_generate_reflection("day", date(2026, 8, 14), date(2026, 8, 15))

    assert counting_groq == []
    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM reflection_cache").fetchone()["n"]
    assert count == 0


def test_failed_generation_does_not_overwrite_existing_cache(isolated_db: None, counting_groq: list) -> None:
    start, end = date(2026, 8, 10), date(2026, 8, 17)
    reflection.get_or_generate_reflection("week", start, end)

    _add_event(1, "created", "2026-08-12T09:00:00+03:00")  # invalidate the cache

    import command_center.triage as triage_module

    def _raise(messages, max_tokens=1024):
        raise TriageProviderError("boom")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(triage_module, "run_groq_chat", _raise)
        with pytest.raises(TriageProviderError):
            reflection.get_or_generate_reflection("week", start, end)

    with db.session() as conn:
        row = conn.execute(
            "SELECT generated_text FROM reflection_cache WHERE period_type='week' AND period_start=?",
            (start.isoformat(),),
        ).fetchone()
    assert row["generated_text"] == "Reflection #1"  # untouched by the failed attempt
