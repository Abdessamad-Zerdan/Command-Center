from datetime import date
from pathlib import Path

import httplib2
import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from command_center import auth, db, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.calendar_view import grid
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


# --- grid.py pure date math --------------------------------------------------


def test_month_bounds_mid_year() -> None:
    start, end = grid.month_bounds(date(2026, 8, 1))
    assert start == date(2026, 8, 1)
    assert end == date(2026, 9, 1)


def test_month_bounds_december_rolls_into_next_year() -> None:
    start, end = grid.month_bounds(date(2026, 12, 1))
    assert end == date(2027, 1, 1)


def test_build_weeks_covers_the_whole_month_and_is_rectangular() -> None:
    weeks = grid.build_weeks(date(2026, 8, 1))
    all_days = [d for week in weeks for d in week]
    assert all(len(week) == 7 for week in weeks)
    # Every day of August 2026 is present.
    for day in range(1, 32):
        assert date(2026, 8, day) in all_days
    # Every week starts on a Monday.
    assert all(week[0].weekday() == 0 for week in weeks)


def test_build_weeks_when_month_starts_on_monday_needs_no_leading_days() -> None:
    # 2026-06-01 is a Monday.
    weeks = grid.build_weeks(date(2026, 6, 1))
    assert weeks[0][0] == date(2026, 6, 1)


def test_shift_month_forward_and_backward_across_year_boundary() -> None:
    assert grid.shift_month(date(2026, 12, 1), 1) == date(2027, 1, 1)
    assert grid.shift_month(date(2027, 1, 1), -1) == date(2026, 12, 1)
    assert grid.shift_month(date(2026, 3, 1), -1) == date(2026, 2, 1)


# --- GET /calendar -------------------------------------------------------------


def test_calendar_page_loads_for_current_month_by_default(client: TestClient) -> None:
    response = client.get("/calendar")
    assert response.status_code == 200


def test_calendar_page_accepts_an_explicit_month(client: TestClient) -> None:
    response = client.get("/calendar?month=2026-03")
    assert response.status_code == 200
    assert "March 2026" in response.text


def test_calendar_page_rejects_a_malformed_month(client: TestClient) -> None:
    response = client.get("/calendar?month=not-a-month")
    assert response.status_code == 400


def test_calendar_is_linked_from_the_nav(client: TestClient) -> None:
    response = client.get("/brief")
    assert 'href="/calendar"' in response.text


def test_calendar_shows_not_connected_banner_when_google_is_not_linked(
    client: TestClient,
) -> None:
    response = client.get("/calendar")
    assert "isn't connected" in response.text


def test_calendar_shows_events_from_google_in_the_right_day_cell(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_google(monkeypatch)

    def fake_fetch_month_events(self, start, end):
        return [{"title": "Team sync", "date": "2026-03-10", "time": "09:00", "link": ""}]

    monkeypatch.setattr(CalendarSource, "__init__", lambda self, credentials: None)
    monkeypatch.setattr(CalendarSource, "fetch_month_events", fake_fetch_month_events)

    response = client.get("/calendar?month=2026-03")

    assert response.status_code == 200
    assert "Team sync" in response.text
    assert "isn't connected" not in response.text


def test_calendar_degrades_gracefully_on_a_google_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_google(monkeypatch)

    def fake_fetch_month_events(self, start, end):
        raise HttpError(httplib2.Response({"status": 500}), b"server error")

    monkeypatch.setattr(CalendarSource, "__init__", lambda self, credentials: None)
    monkeypatch.setattr(CalendarSource, "fetch_month_events", fake_fetch_month_events)

    response = client.get("/calendar?month=2026-03")

    assert response.status_code == 200
    assert "Couldn" in response.text  # "Couldn't reach Google Calendar..."


def test_calendar_shows_a_pending_tasks_due_date_in_its_cell(client: TestClient) -> None:
    queries.create_manual_item("2026-03-01", "urgent", "Renew passport", due_date="2026-03-15")
    response = client.get("/calendar?month=2026-03")
    assert "Renew passport" in response.text


def test_calendar_excludes_tasks_due_well_outside_the_grid(client: TestClient) -> None:
    # March 2026's grid pads a few trailing days into early April to
    # complete its last week (March 31 is a Tuesday) — May is safely
    # outside that padding regardless of which weekday the month starts on.
    queries.create_manual_item("2026-03-01", "urgent", "May task", due_date="2026-05-15")
    response = client.get("/calendar?month=2026-03")
    assert "May task" not in response.text


def test_calendar_excludes_a_done_task_even_with_a_due_date_in_range(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-03-01", "urgent", "Finished thing", due_date="2026-03-15")
    queries.set_item_status(item_id, "done")
    response = client.get("/calendar?month=2026-03")
    assert "Finished thing" not in response.text


# --- PATCH /items/{item_id}/due-date -------------------------------------------


def test_update_due_date_persists(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-03-01", "urgent", "Book flight")
    response = client.patch(f"/items/{item_id}/due-date", json={"due_date": "2026-03-20"})
    assert response.status_code == 200
    with db.session() as conn:
        due = conn.execute("SELECT due_date FROM items WHERE id = ?", (item_id,)).fetchone()["due_date"]
    assert due == "2026-03-20"


def test_update_due_date_can_clear_it(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-03-01", "urgent", "Book flight", due_date="2026-03-20")
    response = client.patch(f"/items/{item_id}/due-date", json={"due_date": None})
    assert response.status_code == 200
    with db.session() as conn:
        due = conn.execute("SELECT due_date FROM items WHERE id = ?", (item_id,)).fetchone()["due_date"]
    assert due is None


def test_update_due_date_404s_for_unknown_item(client: TestClient) -> None:
    response = client.patch("/items/999999/due-date", json={"due_date": "2026-03-20"})
    assert response.status_code == 404


def test_update_due_date_rejects_a_malformed_date(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-03-01", "urgent", "Book flight")
    response = client.patch(f"/items/{item_id}/due-date", json={"due_date": "not-a-date"})
    assert response.status_code == 400


def test_calendar_day_cells_wire_up_the_drop_handler_to_due_date(client: TestClient) -> None:
    response = client.get("/calendar")
    assert "/due-date" in response.text
    assert "dataTransfer.getData" in response.text


def test_calendar_day_cells_wire_up_the_quick_add_form(client: TestClient) -> None:
    response = client.get("/calendar")
    assert "/calendar/tasks" in response.text
    assert "+ task" in response.text


# --- POST /calendar/tasks (quick-add + Google sync) ----------------------------


def test_quick_add_creates_the_task_and_syncs_to_google(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connect_google(monkeypatch)

    def fake_create_event(self, title, start, end, description=""):
        return {"id": "evt-quick", "htmlLink": "https://calendar.google.com/evt-quick"}

    monkeypatch.setattr(CalendarSource, "__init__", lambda self, credentials: None)
    monkeypatch.setattr(CalendarSource, "create_event", fake_create_event)

    response = client.post(
        "/calendar/tasks",
        json={"lane": "tasks_due", "title": "Renew passport", "due_date": "2026-08-20"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["calendar_synced"] is True
    assert body["link"] == "https://calendar.google.com/evt-quick"

    item = queries.get_item_for_calendar(body["id"])
    assert item["title"] == "Renew passport"
    assert item["due_date"] == "2026-08-20"
    assert item["calendar_event_id"] == "evt-quick"
    with db.session() as conn:
        lane = conn.execute("SELECT lane FROM items WHERE id = ?", (body["id"],)).fetchone()["lane"]
    assert lane == "tasks_due"


def test_quick_add_still_creates_the_task_when_google_is_not_connected(
    client: TestClient,
) -> None:
    response = client.post(
        "/calendar/tasks",
        json={"lane": "urgent", "title": "Book flight", "due_date": "2026-08-20"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["calendar_synced"] is False
    assert "Google" in body["calendar_error"]

    item = queries.get_item_for_calendar(body["id"])
    assert item["title"] == "Book flight"
    assert item["calendar_event_id"] is None


def test_quick_add_rejects_unknown_lane(client: TestClient) -> None:
    response = client.post(
        "/calendar/tasks", json={"lane": "not-a-lane", "title": "X", "due_date": "2026-08-20"}
    )
    assert response.status_code == 400


def test_quick_add_rejects_blank_title(client: TestClient) -> None:
    response = client.post(
        "/calendar/tasks", json={"lane": "urgent", "title": "   ", "due_date": "2026-08-20"}
    )
    assert response.status_code == 400


def test_quick_add_rejects_a_malformed_due_date(client: TestClient) -> None:
    response = client.post(
        "/calendar/tasks", json={"lane": "urgent", "title": "X", "due_date": "not-a-date"}
    )
    assert response.status_code == 400


def test_quick_added_task_appears_on_the_calendar_grid(client: TestClient) -> None:
    client.post(
        "/calendar/tasks", json={"lane": "urgent", "title": "Grid-visible task", "due_date": "2026-08-20"}
    )
    response = client.get("/calendar?month=2026-08")
    assert "Grid-visible task" in response.text


# --- click-to-open task detail popup -------------------------------------------


def test_task_chips_wire_up_click_to_open_the_modal(client: TestClient) -> None:
    queries.create_manual_item("2026-08-01", "urgent", "Click me", due_date="2026-08-20")
    response = client.get("/calendar?month=2026-08")
    # Regression guard: a task chip has draggable="true", and a native
    # browser drag gesture silently suppresses that same element's own
    # `click` event — confirmed live, a real mouse click never opened
    # the popup even though a synthetic .click() call did. Chips must
    # detect "was this a click" themselves via mousedown/mouseup
    # position instead of trusting the native (and here, unreliable)
    # click event.
    assert "@click='$dispatch(\"task-modal-open\"" not in response.text
    assert '@mouseup=\'if (Math.abs($event.clientX - downX) < 4 && Math.abs($event.clientY - downY) < 4) { $dispatch("task-modal-open"' in response.text
    assert '@mousedown="downX = $event.clientX; downY = $event.clientY"' in response.text


def test_today_shortcut_button_does_not_bubble_into_the_modal_open_handler(
    client: TestClient,
) -> None:
    # The →today button only renders for a task NOT due today — it
    # sits inside the draggable chip, and without stopping its own
    # mousedown/mouseup, clicking it would also satisfy the chip's
    # movement-threshold check and spuriously pop the detail modal open
    # right after the due-date change.
    queries.create_manual_item("2026-08-01", "urgent", "Not due today", due_date="2026-08-05")
    response = client.get("/calendar?month=2026-08")
    assert "@mousedown.stop" in response.text
    assert "@mouseup.stop" in response.text


def test_calendar_page_defines_the_task_modal_component_and_listener(client: TestClient) -> None:
    response = client.get("/calendar")
    assert "function taskModal()" in response.text
    assert '@task-modal-open.window="onOpen($event.detail)"' in response.text


def test_quick_add_dispatches_the_modal_open_event_on_success(client: TestClient) -> None:
    response = client.get("/calendar")
    assert "$dispatch('task-modal-open'" in response.text
    assert "_justAdded: true" in response.text
