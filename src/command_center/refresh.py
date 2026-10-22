"""Runs the full pipeline (pull + triage, about a minute) in the background.

Startup used to block on it — no page at all until the day's first pull
finished — and "Re-run triage now" held the request open just as long.
Both now start a background run here and return at once: the brief shows
the latest brief it has, with a "Refreshing…" banner that polls
/brief/refresh-status and reloads when the run is done.

Single-flight: a second start() while one run is going is a no-op, so a
double click (or startup + a click) never pulls twice in parallel.
State is per process and in memory — enough for one local app.
"""

import logging
import threading
from datetime import datetime
from typing import Any

from command_center import pipeline
from command_center.calendar_view import router as calendar_view_router
from command_center.config import TZ

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "started_at": None, "finished_at": None, "error": None}


def status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def _run(force: bool) -> None:
    error = None
    try:
        pipeline.run(force=force)
        # A pull may have changed the calendar; have /calendar's month
        # ready rather than fetching it when the page opens.
        calendar_view_router.warm_current_month()
    except Exception as exc:  # deliberate: a failed pull is reported in status(), never crashes the thread
        logger.exception("Background refresh failed")
        error = str(exc) or exc.__class__.__name__
    with _lock:
        _state.update(running=False, finished_at=datetime.now(TZ).isoformat(), error=error)


def start(force: bool = False) -> bool:
    """Starts a background run; False if one is already running."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, started_at=datetime.now(TZ).isoformat(), finished_at=None, error=None)
    threading.Thread(target=_run, args=(force,), name="brief-refresh", daemon=True).start()
    return True
