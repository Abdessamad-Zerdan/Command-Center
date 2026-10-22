"""calendar_view/month_cache.py: /calendar shows cached Google events at
once and refreshes them in the background, instead of waiting ~2.5 s on
Google every visit."""

import threading
import time
from datetime import date, datetime, timedelta

import pytest

from command_center.calendar_view import month_cache
from command_center.config import TZ

START, END = date(2026, 9, 28), date(2026, 11, 9)


class _Google:
    def __init__(self) -> None:
        self.calls = 0
        self.events = [{"title": "Standup", "date": "2026-10-22", "time": "09:00", "link": ""}]
        self.fail = False
        self.gate: threading.Event | None = None

    def __call__(self, start: date, end: date) -> list[dict]:
        self.calls += 1
        if self.gate:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("Google unreachable")
        return list(self.events)


def _age(minutes: float) -> None:
    """Makes the cached entry look `minutes` old."""
    key = (START.isoformat(), END.isoformat())
    fetched_at, events = month_cache._entries[key]
    month_cache._entries[key] = (datetime.now(TZ) - timedelta(minutes=minutes), events)


def _wait_for_refresh() -> None:
    deadline = time.monotonic() + 5
    while month_cache._refreshing and time.monotonic() < deadline:
        time.sleep(0.01)


def test_first_visit_fetches_then_fresh_visits_use_the_cache() -> None:
    google = _Google()

    events, _ = month_cache.get(START, END, google)
    again, _ = month_cache.get(START, END, google)

    assert events == again == google.events
    assert google.calls == 1


def test_a_stale_entry_is_served_at_once_and_refreshed_in_the_background() -> None:
    google = _Google()
    month_cache.get(START, END, google)
    _age(10)
    google.events = [{"title": "Moved meeting", "date": "2026-10-23", "time": None, "link": ""}]
    google.gate = threading.Event()  # hold the refresh until we've checked what was served

    served, fetched_at = month_cache.get(START, END, google)

    assert served[0]["title"] == "Standup"                    # the old copy, without waiting
    assert datetime.now(TZ) - fetched_at >= timedelta(minutes=9)  # and it says how old it is
    google.gate.set()
    _wait_for_refresh()
    assert month_cache.get(START, END, google)[0][0]["title"] == "Moved meeting"


def test_only_one_background_refresh_runs_at_a_time() -> None:
    google = _Google()
    month_cache.get(START, END, google)
    _age(10)
    google.gate = threading.Event()

    for _ in range(5):
        month_cache.get(START, END, google)
    google.gate.set()
    _wait_for_refresh()

    assert google.calls == 2  # the first fetch + one refresh


def test_a_failed_background_refresh_keeps_the_cached_events() -> None:
    google = _Google()
    month_cache.get(START, END, google)
    _age(10)
    google.fail = True

    month_cache.get(START, END, google)
    _wait_for_refresh()

    assert month_cache.get(START, END, google)[0] == google.events


def test_with_nothing_cached_a_failure_is_raised_for_the_page_to_report() -> None:
    google = _Google()
    google.fail = True
    with pytest.raises(RuntimeError):
        month_cache.get(START, END, google)


def test_clear_forces_the_next_visit_to_fetch() -> None:
    google = _Google()
    month_cache.get(START, END, google)
    month_cache.clear()
    month_cache.get(START, END, google)
    assert google.calls == 2


def test_warm_fills_the_cache_in_the_background() -> None:
    google = _Google()
    month_cache.warm(START, END, google)
    _wait_for_refresh()

    month_cache.get(START, END, google)

    assert google.calls == 1  # the visit used what warm() fetched
