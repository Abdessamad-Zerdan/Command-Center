"""Google Calendar events for /calendar's month grid, cached in memory.

Every visit used to fetch the month from Google before rendering anything
— about 2.5 s on each page load, against the rule that a page never waits
on the network. Now (stale-while-revalidate):

- fresh (younger than FRESH_FOR): served as is;
- older: served at once, and refreshed in a background thread so the
  next visit is current — the page says when the events are from;
- never fetched: fetched in the request (the first visit after a start,
  made rare by warm() running at startup and after every pull).

The app's own writes to Google Calendar call clear(), so an event added
from here shows straight away. Per process and in memory, like
refresh.py's state — enough for one local app.
"""

import logging
import threading
from datetime import date, datetime, timedelta
from typing import Callable

from command_center.config import TZ

logger = logging.getLogger(__name__)

FRESH_FOR = timedelta(minutes=5)

_lock = threading.Lock()
_entries: dict[tuple[str, str], tuple[datetime, list[dict]]] = {}
_refreshing: set[tuple[str, str]] = set()


def _key(start: date, end: date) -> tuple[str, str]:
    return start.isoformat(), end.isoformat()


def get(start: date, end: date, fetch: Callable[[date, date], list[dict]]) -> tuple[list[dict], datetime]:
    """(events, when they were fetched) for [start, end). `fetch` is the
    real Google call; it raises on failure, which propagates only when
    there is nothing cached to show instead."""
    key = _key(start, end)
    with _lock:
        cached = _entries.get(key)
    now = datetime.now(TZ)
    if cached is None:
        events = fetch(start, end)
        with _lock:
            _entries[key] = (now, events)
        return events, now
    fetched_at, events = cached
    if now - fetched_at >= FRESH_FOR:
        _refresh_in_background(key, start, end, fetch)
    return events, fetched_at


def _refresh_in_background(key, start: date, end: date, fetch) -> None:
    with _lock:
        if key in _refreshing:
            return  # single-flight: one refresh per month at a time
        _refreshing.add(key)

    def run() -> None:
        try:
            events = fetch(start, end)
            with _lock:
                _entries[key] = (datetime.now(TZ), events)
        # Deliberate: a failed background refresh keeps the cached events
        # (the page already says how old they are) and must not crash
        # the thread.
        except Exception:
            logger.exception("Background refresh of calendar events failed")
        finally:
            with _lock:
                _refreshing.discard(key)

    threading.Thread(target=run, name="calendar-month-refresh", daemon=True).start()


def warm(start: date, end: date, fetch) -> None:
    """Fetches [start, end) in the background so the next /calendar visit
    is instant. Used at startup and after each pull."""
    _refresh_in_background(_key(start, end), start, end, fetch)


def clear() -> None:
    with _lock:
        _entries.clear()
