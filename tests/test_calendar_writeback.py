from pathlib import Path

import pytest
from fastapi.testclient import TestClient
import httplib2
from googleapiclient.errors import HttpError

from command_center import auth, db, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.setup_wizard import status as setup_status
from command_center.sources.calendar import CalendarSource


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def _connect_google(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: True)
    monkeypatch.setattr(auth, "get_google_credentials", lambda: "fake-creds")


def test_add_to_calendar_404s_for_unknown_item(client: TestClient) -> None:
    response = client.post("/items/99999/add-to-calendar")
    assert response.status_code == 404


def test_add_to_calendar_rejects_google_not_connected(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Book flight", due_date="2026-08-21")
    response = client.post(f"/items/{item_id}/add-to-calendar")
    assert response.status_code == 400
    assert "Google" in response.json()["detail"]


def test_add_to_calendar_rejects_an_item_with_no_date(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_google(monkeypatch)
    item_id = queries.create_manual_item("2026-08-14", "urgent", "No date at all")

    response = client.post(f"/items/{item_id}/add-to-calendar")

    assert response.status_code == 400
    assert "no due date" in response.json()["detail"].lower()


def test_add_to_calendar_rejects_a_calendar_sourced_item(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_google(monkeypatch)
    with db.session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO briefs (brief_date, generated_at, degraded_lanes) "
            "VALUES ('2026-08-14', '2026-08-14T10:00:00', '[]')"
        )
        cursor = conn.execute(
            "INSERT INTO items (brief_date, lane, source, source_id, title, why_it_matters, "
            "suggested_next_step, priority, deep_link, due_date, status, created_at) "
            "VALUES ('2026-08-14', 'meeting_prep', 'calendar', 'cal1', 'Standup', '', '', 2, '', "
            "'2026-08-14', 'pending', '2026-08-14T10:00:00')"
        )
        item_id = cursor.lastrowid

    response = client.post(f"/items/{item_id}/add-to-calendar")

    assert response.status_code == 400
    assert "already" in response.json()["detail"].lower()


def test_add_to_calendar_creates_an_all_day_event_from_due_date(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_google(monkeypatch)
    calls = []

    def fake_create_event(self, title, start, end, description=""):
        calls.append({"title": title, "start": start, "end": end})
        return {"id": "evt-1", "htmlLink": "https://calendar.google.com/evt-1"}

    monkeypatch.setattr(CalendarSource, "__init__", lambda self, credentials: None)
    monkeypatch.setattr(CalendarSource, "create_event", fake_create_event)
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Book flight", due_date="2026-08-21")

    response = client.post(f"/items/{item_id}/add-to-calendar")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "link": "https://calendar.google.com/evt-1"}
    assert calls[0]["start"] == {"date": "2026-08-21"}
    assert calls[0]["end"] == {"date": "2026-08-22"}
    item = queries.get_item_for_calendar(item_id)
    assert item["calendar_event_id"] == "evt-1"
    assert item["calendar_link"] == "https://calendar.google.com/evt-1"


def test_add_to_calendar_creates_a_timed_event_from_schedule(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_google(monkeypatch)
    calls = []

    def fake_create_event(self, title, start, end, description=""):
        calls.append({"start": start, "end": end})
        return {"id": "evt-2", "htmlLink": "https://calendar.google.com/evt-2"}

    monkeypatch.setattr(CalendarSource, "__init__", lambda self, credentials: None)
    monkeypatch.setattr(CalendarSource, "create_event", fake_create_event)
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Deep work block")
    with db.session() as conn:
        conn.execute(
            "UPDATE items SET scheduled_start = ?, scheduled_end = ? WHERE id = ?",
            ("2026-08-21T10:00:00+03:00", "2026-08-21T11:00:00+03:00", item_id),
        )

    response = client.post(f"/items/{item_id}/add-to-calendar")

    assert response.status_code == 200
    assert calls[0]["start"]["dateTime"] == "2026-08-21T10:00:00+03:00"
    assert calls[0]["end"]["dateTime"] == "2026-08-21T11:00:00+03:00"


def test_add_to_calendar_is_idempotent_on_a_second_click(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_google(monkeypatch)
    calls = []

    def fake_create_event(self, title, start, end, description=""):
        calls.append(1)
        return {"id": "evt-3", "htmlLink": "https://calendar.google.com/evt-3"}

    monkeypatch.setattr(CalendarSource, "__init__", lambda self, credentials: None)
    monkeypatch.setattr(CalendarSource, "create_event", fake_create_event)
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Book flight", due_date="2026-08-21")

    first = client.post(f"/items/{item_id}/add-to-calendar")
    second = client.post(f"/items/{item_id}/add-to-calendar")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["link"] == "https://calendar.google.com/evt-3"
    assert len(calls) == 1  # no duplicate event created on the second click


def test_add_to_calendar_maps_a_403_to_a_reauth_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_google(monkeypatch)

    def fake_create_event(self, title, start, end, description=""):
        raise HttpError(httplib2.Response({"status": 403}), b"insufficient scope")

    monkeypatch.setattr(CalendarSource, "__init__", lambda self, credentials: None)
    monkeypatch.setattr(CalendarSource, "create_event", fake_create_event)
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Book flight", due_date="2026-08-21")

    response = client.post(f"/items/{item_id}/add-to-calendar")

    assert response.status_code == 403
    assert "reconnect" in response.json()["detail"].lower()


def test_add_to_calendar_maps_other_http_errors_to_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_google(monkeypatch)

    def fake_create_event(self, title, start, end, description=""):
        raise HttpError(httplib2.Response({"status": 500}), b"server error")

    monkeypatch.setattr(CalendarSource, "__init__", lambda self, credentials: None)
    monkeypatch.setattr(CalendarSource, "create_event", fake_create_event)
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Book flight", due_date="2026-08-21")

    response = client.post(f"/items/{item_id}/add-to-calendar")

    assert response.status_code == 502
