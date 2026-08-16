from datetime import date
from pathlib import Path

import pytest

from command_center import db, triage
from command_center.finances import reflection
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


def _add_entry(entry_date: str, amount: float = 10.0) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO finance_entries (amount, category, type, entry_date, note, created_at) "
            "VALUES (?, 'Food', 'spend', ?, '', ?)",
            (amount, entry_date, entry_date),
        )


def test_cache_miss_calls_groq_once_and_stores_row(isolated_db: None, counting_groq: list) -> None:
    start, end = date(2026, 8, 1), date(2026, 9, 1)

    text = reflection.get_or_generate_reflection(start, end)

    assert text == "Reflection #1"
    assert len(counting_groq) == 1
    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM finance_reflection_cache WHERE month_start = ?", (start.isoformat(),)
        ).fetchone()
    assert row["generated_text"] == "Reflection #1"
    assert row["entry_count"] == 0


def test_cache_hit_does_not_call_groq_again(isolated_db: None, counting_groq: list) -> None:
    start, end = date(2026, 8, 1), date(2026, 9, 1)

    first = reflection.get_or_generate_reflection(start, end)
    second = reflection.get_or_generate_reflection(start, end)

    assert second == first
    assert len(counting_groq) == 1


def test_new_entry_before_end_invalidates_cache(isolated_db: None, counting_groq: list) -> None:
    start, end = date(2026, 8, 1), date(2026, 9, 1)

    reflection.get_or_generate_reflection(start, end)
    _add_entry("2026-08-15")  # inside the window, < end
    second = reflection.get_or_generate_reflection(start, end)

    assert len(counting_groq) == 2
    assert second == "Reflection #2"
    with db.session() as conn:
        row = conn.execute(
            "SELECT entry_count FROM finance_reflection_cache WHERE month_start = ?",
            (start.isoformat(),),
        ).fetchone()
    assert row["entry_count"] == 1


def test_force_regenerates_even_when_entry_count_unchanged(
    isolated_db: None, counting_groq: list
) -> None:
    start, end = date(2026, 8, 1), date(2026, 9, 1)

    reflection.get_or_generate_reflection(start, end)
    second = reflection.get_or_generate_reflection(start, end, force=True)

    assert len(counting_groq) == 2
    assert second == "Reflection #2"


def test_entry_added_after_end_does_not_invalidate_cache(
    isolated_db: None, counting_groq: list
) -> None:
    start, end = date(2026, 8, 1), date(2026, 9, 1)

    reflection.get_or_generate_reflection(start, end)
    _add_entry("2026-09-01")  # exactly at end — excluded by the < comparison
    _add_entry("2026-10-01")  # well after end
    second = reflection.get_or_generate_reflection(start, end)

    assert len(counting_groq) == 1  # no second Groq call — cache still valid
    assert second == "Reflection #1"


def test_failed_generation_does_not_overwrite_existing_cache(
    isolated_db: None, counting_groq: list
) -> None:
    start, end = date(2026, 8, 1), date(2026, 9, 1)
    reflection.get_or_generate_reflection(start, end)

    _add_entry("2026-08-15")  # invalidate the cache

    import command_center.triage as triage_module

    def _raise(messages, max_tokens=1024):
        raise TriageProviderError("boom")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(triage_module, "run_groq_chat", _raise)
        with pytest.raises(TriageProviderError):
            reflection.get_or_generate_reflection(start, end)

    with db.session() as conn:
        row = conn.execute(
            "SELECT generated_text FROM finance_reflection_cache WHERE month_start = ?",
            (start.isoformat(),),
        ).fetchone()
    assert row["generated_text"] == "Reflection #1"  # untouched by the failed attempt


def test_generate_reflection_says_no_entries_when_both_months_empty(
    isolated_db: None, counting_groq: list
) -> None:
    start, end = date(2026, 8, 1), date(2026, 9, 1)

    reflection.get_or_generate_reflection(start, end)

    sent = counting_groq[0][1]["content"]
    assert "No entries were recorded in either month." in sent
