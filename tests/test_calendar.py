from datetime import datetime, timedelta, timezone

from command_center.config import TZ
from command_center.sources.calendar import (
    CalendarSource,
    _event_links,
    _event_to_raw_item,
    _event_to_timeline_row,
    _parse_event_time,
)


def test_parse_event_time_handles_tz_aware_value() -> None:
    # Computed against TZ rather than hardcoded to a specific offset —
    # this must hold regardless of what APP_TIMEZONE the environment
    # running the test actually has set (e.g. a fresh CI checkout with
    # no .env defaults to UTC, not this dev machine's Europe/Istanbul).
    dt = _parse_event_time("2026-08-16T10:00:00+03:00")
    expected = datetime(2026, 8, 16, 10, 0, tzinfo=timezone(timedelta(hours=3))).astimezone(TZ)
    assert dt == expected


def test_parse_event_time_assumes_app_timezone_when_naive() -> None:
    dt = _parse_event_time("2026-08-16T10:00:00")
    assert dt is not None
    assert dt.tzinfo == TZ
    assert dt.hour == 10


def test_parse_event_time_returns_none_for_an_all_day_date_string() -> None:
    # All-day events give "2026-08-16" (a bare date), not a datetime —
    # fromisoformat on that alone actually parses fine in 3.11+, so this
    # instead exercises the real failure mode: a garbage value.
    assert _parse_event_time("not-a-real-timestamp") is None


def test_event_links_collects_hangout_link_and_attachment_urls() -> None:
    ev = {
        "hangoutLink": "https://meet.google.com/abc",
        "attachments": [{"fileUrl": "https://docs.google.com/1"}, {"title": "no url"}],
    }
    assert _event_links(ev) == ["https://meet.google.com/abc", "https://docs.google.com/1"]


def test_event_links_empty_when_neither_present() -> None:
    assert _event_links({}) == []


def test_event_to_raw_item_includes_attendees_links_and_truncated_description() -> None:
    ev = {
        "id": "ev1",
        "summary": "Standup",
        "htmlLink": "https://calendar.google.com/event?eid=1",
        "attendees": [{"email": "a@x.com"}, {"email": "b@x.com"}, {}],
        "hangoutLink": "https://meet.google.com/abc",
        "description": "Q" * 500,
        "start": {"dateTime": "2026-08-16T09:00:00+03:00"},
        "end": {"dateTime": "2026-08-16T09:30:00+03:00"},
    }

    item = _event_to_raw_item(ev)

    assert item.source == "calendar"
    assert item.source_id == "ev1"
    assert item.title == "Standup"
    assert "Attendees: a@x.com, b@x.com" in item.body
    assert "Links: https://meet.google.com/abc" in item.body
    assert item.body.count("Q") == 400  # description truncated to 400 chars
    assert item.metadata["attendees"] == ["a@x.com", "b@x.com"]
    assert item.metadata["start_time"] == "2026-08-16T09:00:00+03:00"


def test_event_to_raw_item_defaults_missing_title() -> None:
    ev = {"id": "ev2", "start": {"date": "2026-08-16"}, "end": {"date": "2026-08-17"}}
    item = _event_to_raw_item(ev)
    assert item.title == "(no title)"
    assert item.metadata["start_time"] == "2026-08-16"


def test_event_to_timeline_row_shapes_fields() -> None:
    ev = {
        "summary": "1:1",
        "start": {"dateTime": "2026-08-16T14:00:00+03:00"},
        "end": {"dateTime": "2026-08-16T14:30:00+03:00"},
        "attendees": [{"email": "a@x.com"}],
        "hangoutLink": "https://meet.google.com/xyz",
    }
    row = _event_to_timeline_row(ev)
    assert row["title"] == "1:1"
    assert row["start_time"] == "2026-08-16T14:00:00+03:00"
    assert row["link"] == "https://meet.google.com/xyz"
    assert row["attendees"] == '["a@x.com"]'


class _FakeExecutable:
    def __init__(self, result: dict) -> None:
        self._result = result

    def execute(self) -> dict:
        return self._result


class _FakeEvents:
    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def list(self, **kwargs) -> _FakeExecutable:
        return _FakeExecutable({"items": self._items})


class _FakeCalendarService:
    def __init__(self, items: list[dict]) -> None:
        self._events = _FakeEvents(items)

    def events(self) -> _FakeEvents:
        return self._events


def test_fetch_with_events_buckets_today_vs_tomorrow_and_caps_tomorrow() -> None:
    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    today_ev = {
        "id": "today1",
        "summary": "Today event",
        "start": {"dateTime": (today_start + timedelta(hours=9)).isoformat()},
        "end": {"dateTime": (today_start + timedelta(hours=10)).isoformat()},
    }
    tomorrow_events = [
        {
            "id": f"tmrw{i}",
            "summary": f"Tomorrow event {i}",
            "start": {"dateTime": (tomorrow_start + timedelta(hours=i)).isoformat()},
            "end": {"dateTime": (tomorrow_start + timedelta(hours=i, minutes=30)).isoformat()},
        }
        for i in range(5)
    ]
    yesterday_ev = {
        "id": "old1",
        "summary": "Should be excluded",
        "start": {"dateTime": (today_start - timedelta(hours=1)).isoformat()},
        "end": {"dateTime": (today_start - timedelta(minutes=30)).isoformat()},
    }

    source = CalendarSource.__new__(CalendarSource)
    source._service = _FakeCalendarService([today_ev, *tomorrow_events, yesterday_ev])

    raw_items, timeline_rows = source.fetch_with_events()

    # today's timeline strip only ever shows today's events
    assert [row["title"] for row in timeline_rows] == ["Today event"]
    # today (1) + tomorrow capped at TOMORROW_LIMIT (3) = 4, yesterday excluded
    assert len(raw_items) == 4
    assert {item.source_id for item in raw_items} == {"today1", "tmrw0", "tmrw1", "tmrw2"}


def test_fetch_with_events_skips_events_with_unparseable_start() -> None:
    ev = {"id": "bad1", "summary": "Bad event", "start": {}, "end": {}}
    source = CalendarSource.__new__(CalendarSource)
    source._service = _FakeCalendarService([ev])

    raw_items, timeline_rows = source.fetch_with_events()

    assert raw_items == []
    assert timeline_rows == []


def test_fetch_delegates_to_fetch_with_events_and_returns_only_raw_items() -> None:
    source = CalendarSource.__new__(CalendarSource)
    source._service = _FakeCalendarService([])

    assert source.fetch() == []
