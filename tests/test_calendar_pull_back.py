"""calendar_sync.pull_back_edits: a task pushed to Google Calendar picks
up the date (and, if manual, the title) of its event when the user moved
or renamed it there — without it a moved exam stayed "overdue"."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from googleapiclient.errors import HttpError

from command_center import calendar_sync, db, queries


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


class _FakeSource:
    def __init__(self, events: dict) -> None:
        self.events = events
        self.asked: list[str] = []

    def get_event(self, event_id: str):
        self.asked.append(event_id)
        event = self.events[event_id]
        if isinstance(event, Exception):
            raise event
        return event


def _synced_task(title: str, due: str, event_id: str) -> int:
    item_id = queries.create_manual_item("2026-10-21", "tasks_due", title, due)
    queries.set_item_calendar_event(item_id, event_id, "https://calendar.google.com/x")
    return item_id


def _item(item_id: int) -> dict:
    return next(i for i in queries.list_pending_calendar_synced_items() if i["id"] == item_id)


def test_a_moved_and_renamed_event_updates_the_task(isolated_db: None) -> None:
    item_id = _synced_task("MATH 2 ARA SINAV", "2026-10-21", "ev1")
    source = _FakeSource({"ev1": {"summary": "MATH 2 ARA SINAV 15:00", "start": {"date": "2026-11-09"}}})

    assert calendar_sync.pull_back_edits(source) == 1

    item = _item(item_id)
    assert item["due_date"] == "2026-11-09"
    assert item["title"] == "MATH 2 ARA SINAV 15:00"
    assert queries.list_overdue_tasks("2026-10-22") == []


def test_a_timed_event_uses_its_local_date(isolated_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calendar_sync, "TZ", ZoneInfo("Europe/Istanbul"))
    item_id = _synced_task("Exam", "2026-10-21", "ev1")
    # 23:30 UTC on the 8th is already the 9th in Istanbul (UTC+3).
    source = _FakeSource({"ev1": {"summary": "Exam", "start": {"dateTime": "2026-11-08T23:30:00Z"}}})

    calendar_sync.pull_back_edits(source)

    assert _item(item_id)["due_date"] == "2026-11-09"


def test_an_unchanged_event_changes_nothing(isolated_db: None) -> None:
    _synced_task("Exam", "2026-11-09", "ev1")
    source = _FakeSource({"ev1": {"summary": "Exam", "start": {"date": "2026-11-09"}}})

    assert calendar_sync.pull_back_edits(source) == 0


def test_a_deleted_or_cancelled_event_leaves_the_task_alone(isolated_db: None) -> None:
    gone = _synced_task("Gone", "2026-10-21", "ev1")
    cancelled = _synced_task("Cancelled", "2026-10-21", "ev2")
    source = _FakeSource({"ev1": None, "ev2": {"status": "cancelled", "summary": "x", "start": {"date": "2026-12-01"}}})

    assert calendar_sync.pull_back_edits(source) == 0
    assert _item(gone)["due_date"] == "2026-10-21"
    assert _item(cancelled)["title"] == "Cancelled"


def test_one_failing_event_does_not_stop_the_others(isolated_db: None) -> None:
    class _Resp(dict):
        status = 500
        reason = "boom"

    _synced_task("Broken", "2026-10-21", "bad")
    ok = _synced_task("Fine", "2026-10-21", "good")
    source = _FakeSource({
        "bad": HttpError(_Resp(), b"boom"),
        "good": {"summary": "Fine", "start": {"date": "2026-11-01"}},
    })

    assert calendar_sync.pull_back_edits(source) == 1
    assert _item(ok)["due_date"] == "2026-11-01"


def test_done_tasks_are_not_re_read(isolated_db: None) -> None:
    item_id = _synced_task("Done", "2026-10-21", "ev1")
    queries.set_item_status(item_id, "done")
    source = _FakeSource({"ev1": {"summary": "x", "start": {"date": "2026-12-01"}}})

    calendar_sync.pull_back_edits(source)

    assert source.asked == []
