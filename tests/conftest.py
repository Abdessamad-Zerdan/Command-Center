"""Shared test setup.

FastAPI's TestClient sends `Host: testserver`, which host_guard's
TrustedHostMiddleware would refuse with the production default
("localhost,127.0.0.1"). Allow it here, before any test imports the app —
the guard reads ALLOWED_HOSTS once, when app.py is imported. Real runs
never see this; tests that check the guard itself pass their own
base_url (see test_host_guard.py).
"""

import os
from pathlib import Path

import pytest

os.environ["ALLOWED_HOSTS"] = "localhost,127.0.0.1,testserver"


@pytest.fixture(autouse=True)
def _never_touch_the_real_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Points the app at an empty, throwaway database path for every test.

    Without this, a test that forgot its own isolated-DB fixture silently
    read (or wrote!) the developer's real command_center.db — and passed
    locally while failing in CI, where that file doesn't exist. Now such a
    test fails the same way everywhere ("no such table"), and the real
    database is never opened. Tests that need tables still set up their
    own DB as before; their monkeypatch runs after this one and wins.
    """
    from command_center import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "unset-test.db")


@pytest.fixture(autouse=True)
def _reading_picker_keeps_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """reading.pick asks the model which Reading candidates to keep; it
    catches every failure (degrading Reading instead), so a test that
    reached the real model would pass silently — slowly, and billed. By
    default the picker here keeps every candidate in order, so pipeline
    tests see the same Reading items they always did. test_reading.py
    restores the real call to test the picker itself."""
    from command_center import reading

    monkeypatch.setattr(
        reading, "_ask_model",
        lambda candidates, raw_by_key: [{"n": n, "priority": 2} for n in range(len(candidates))],
    )


@pytest.fixture(autouse=True)
def _no_prioritizing_after_pulls(monkeypatch: pytest.MonkeyPatch) -> None:
    """pipeline runs prioritize.run after every pull; its failure is
    caught (degradation), so a test reaching the real model there would
    pass silently. Stubbed for every test; test_prioritize.py restores it."""
    from command_center import prioritize

    monkeypatch.setattr(prioritize, "run", lambda *args, **kwargs: 0)


@pytest.fixture(autouse=True)
def _never_call_the_real_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails any test that would reach the real Groq/OpenAI-compatible
    model — a test must script the model's answer (patch triage.run,
    run_groq_chat or the client). Before this, pages that wrote their AI
    reflection on load made real, billed calls in three tests, which then
    waited out rate limits and slowed the whole suite."""
    import openai

    def _refuse(*args, **kwargs):
        raise RuntimeError("A test reached the real model; script its answer instead.")

    # The client constructor every Groq call builds; a test that fakes the
    # client patches this too, and its patch wins.
    monkeypatch.setattr(openai, "OpenAI", _refuse)


@pytest.fixture(autouse=True)
def _no_background_calendar_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app pre-fetches /calendar's month from Google at startup and
    after pulls (month_cache.warm). Tests that pretend Google is connected
    would otherwise make a real call with this machine's token. Stubbed
    for every test; test_calendar_month_cache.py tests the cache itself.
    Also starts each test with an empty cache."""
    from command_center.calendar_view import month_cache
    from command_center.calendar_view import router as calendar_view_router

    monkeypatch.setattr(calendar_view_router, "warm_current_month", lambda: None)
    month_cache.clear()
