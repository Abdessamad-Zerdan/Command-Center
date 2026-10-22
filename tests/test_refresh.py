"""The pipeline runs in the background (refresh.py): startup and "Re-run
triage now" return at once, /brief shows the latest brief with a
"Refreshing…" banner meanwhile, and only one run happens at a time."""

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, pipeline, queries, refresh
from command_center.assistant import ingest as assistant_ingest
from command_center.config import TZ
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def slow_pipeline(monkeypatch: pytest.MonkeyPatch):
    """A pipeline.run that blocks until the test releases it."""
    release = threading.Event()
    calls: list[bool] = []

    def run(force: bool = False) -> None:
        calls.append(force)
        release.wait(timeout=10)

    monkeypatch.setattr(pipeline, "run", run)
    yield release, calls
    release.set()
    _wait_idle()


def _wait_idle() -> None:
    deadline = time.monotonic() + 10
    while refresh.status()["running"] and time.monotonic() < deadline:
        time.sleep(0.02)


def test_start_is_single_flight(slow_pipeline) -> None:
    release, calls = slow_pipeline
    assert refresh.start() is True
    assert refresh.start() is False  # already running
    assert refresh.status()["running"] is True
    release.set()
    _wait_idle()
    assert calls == [False]
    status = refresh.status()
    assert status["running"] is False and status["error"] is None and status["finished_at"]


def test_a_failed_run_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(force: bool = False) -> None:
        raise RuntimeError("Gmail down")

    monkeypatch.setattr(pipeline, "run", boom)
    refresh.start()
    _wait_idle()
    assert refresh.status()["error"] == "Gmail down"


@pytest.fixture()
def connected_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: True)  # Google "connected"
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center import app as app_module

    # The coordinator scheduler isn't what's under test — keep it from starting threads.
    monkeypatch.setattr(app_module, "BackgroundScheduler", lambda **kw: type("S", (), {
        "add_job": lambda *a, **k: None, "start": lambda self: None, "shutdown": lambda self, **k: None,
    })())
    return app_module.app


def _yesterdays_brief() -> str:
    yesterday = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
    queries.save_triage_results(yesterday, [{
        "lane": "action_items", "source": "gmail", "source_id": "old", "title": "Item from yesterday",
        "why_it_matters": "", "suggested_next_step": "", "priority": 2, "deep_link": "", "due_date": None,
    }], [], [], ["gmail"])
    return yesterday


def test_startup_does_not_wait_for_the_pull(connected_app, slow_pipeline) -> None:
    release, calls = slow_pipeline
    started = time.monotonic()
    with TestClient(connected_app) as client:
        assert time.monotonic() - started < 5  # the pull is still blocked, yet the app is up
        assert refresh.status()["running"] is True
        page = client.get("/brief")
        assert page.status_code == 200 and "Building today's brief" in page.text
        release.set()
    assert calls == [False]


def test_brief_shows_the_latest_brief_while_refreshing(connected_app, slow_pipeline) -> None:
    release, _ = slow_pipeline
    with TestClient(connected_app) as client:
        yesterday = _yesterdays_brief()
        html = client.get("/brief").text
        assert "Item from yesterday" in html
        assert "Refreshing today's brief" in html and f"Showing {yesterday}'s meanwhile" in html
        status = client.get("/brief/refresh-status").json()
        assert status["running"] is True and status["today_ready"] is False
        release.set()


def test_rerun_returns_at_once_and_starts_one_background_run(connected_app, slow_pipeline) -> None:
    release, calls = slow_pipeline
    with TestClient(connected_app) as client:
        release.set()  # let the startup run finish first
        _wait_idle()
        release.clear()
        started = time.monotonic()
        response = client.post("/rerun", follow_redirects=False)
        assert response.status_code == 303 and time.monotonic() - started < 5
        assert refresh.status()["running"] is True
        client.post("/rerun", follow_redirects=False)  # a double click doesn't pull twice
        release.set()
        _wait_idle()
    assert calls == [False, False]  # startup + one rerun


def test_bring_overdue_to_today_uses_today_while_yesterdays_brief_is_shown(
    connected_app, slow_pipeline
) -> None:
    # Today's brief isn't in yet, so yesterday's is on screen — the
    # overdue nudge counts against today, so "Bring to today" must move
    # tasks to today, not to the date of the brief being shown (which
    # left them overdue and made the button look broken).
    release, _ = slow_pipeline
    with TestClient(connected_app) as client:
        yesterday = _yesterdays_brief()
        queries.create_manual_item(yesterday, "tasks_due", "Exam", yesterday)
        today = datetime.now(TZ).date().isoformat()
        html = client.get("/brief").text
        assert "1 item overdue" in html
        assert f"due_date: '{today}'" in html
        assert f"due_date: '{yesterday}'" not in html
        release.set()
