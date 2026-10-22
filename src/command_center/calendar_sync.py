"""Shared "push one item to Google Calendar" logic. Used by both
POST /items/{item_id}/add-to-calendar (app.py) and POST /calendar/tasks
(calendar_view/router.py, which creates a manual task and syncs it to
Google in the same request). Lives in its own module specifically so
neither of those two routers has to import the other — app.py already
imports calendar_view.router, so the reverse would be circular.
"""

import logging
from datetime import datetime, timedelta

from fastapi import HTTPException
from googleapiclient.errors import HttpError

from command_center import auth, queries
from command_center.calendar_view import month_cache
from command_center.config import TZ
from command_center.sources.calendar import CalendarSource

logger = logging.getLogger(__name__)


def sync_item_to_calendar(item: dict) -> dict:
    """item is a queries.get_item_for_calendar() row: needs id, title,
    source, calendar_event_id, calendar_link, scheduled_start,
    scheduled_end, due_date. Returns {"link": ...}. Raises HTTPException
    on any failure — the exact status codes/messages
    /items/{id}/add-to-calendar has always used, just factored out so
    /calendar/tasks's quick-add-and-sync can raise (or catch) them too.
    """
    if item["calendar_event_id"]:
        # Already added — idempotent, just hand back the existing link
        # rather than creating a duplicate event on a second call.
        return {"link": item["calendar_link"]}
    if item["source"] == "calendar":
        raise HTTPException(status_code=400, detail="This item already is a calendar event")

    if item["scheduled_start"] and item["scheduled_end"]:
        start = {"dateTime": item["scheduled_start"], "timeZone": str(TZ)}
        end = {"dateTime": item["scheduled_end"], "timeZone": str(TZ)}
    elif item["due_date"]:
        start = {"date": item["due_date"]}
        end = {"date": (datetime.fromisoformat(item["due_date"]) + timedelta(days=1)).date().isoformat()}
    else:
        raise HTTPException(status_code=400, detail="This item has no due date or scheduled time to add")

    if not auth.has_valid_credentials():
        raise HTTPException(status_code=400, detail="Google isn't connected")
    try:
        credentials = auth.get_google_credentials()
        event = CalendarSource(credentials).create_event(item["title"], start, end)
    except auth.AuthNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HttpError as exc:
        if exc.resp.status == 403:
            raise HTTPException(
                status_code=403,
                detail="Google Calendar rejected this — your saved connection likely predates "
                "write access. Reconnect Google (see SETUP.md) to grant it.",
            ) from exc
        raise HTTPException(status_code=502, detail=f"Google Calendar request failed: {exc}") from exc

    link = event.get("htmlLink", "")
    queries.set_item_calendar_event(item["id"], event["id"], link)
    # The month grid caches Google's events — drop it so the new event
    # shows on the next /calendar load, not after the cache ages out.
    month_cache.clear()
    return {"link": link}


def _event_date(event: dict) -> str | None:
    start = event.get("start") or {}
    if start.get("date"):
        return start["date"]
    if start.get("dateTime"):
        return datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(TZ).date().isoformat()
    return None


def pull_back_edits(source: CalendarSource) -> int:
    """The app pushes a task to Google Calendar once and never looked at
    it again, so moving or renaming the event there left the task on its
    old date — an exam moved to next month still showed as overdue. Each
    calendar pull now re-reads those events and copies a changed date
    (and, for manual tasks, title) back. A deleted or cancelled event
    leaves the task untouched: deleting a calendar entry isn't a sign the
    task is done. Returns how many tasks changed."""
    changed = 0
    for item in queries.list_pending_calendar_synced_items():
        try:
            event = source.get_event(item["calendar_event_id"])
        except HttpError:
            logger.exception("Couldn't re-read calendar event for item %s", item["id"])
            continue
        if event is None or event.get("status") == "cancelled":
            continue
        updated = False
        new_date = _event_date(event)
        if new_date and new_date != item["due_date"]:
            updated = queries.set_item_due_date(item["id"], new_date) or updated
        title = (event.get("summary") or "").strip()
        if title and title != item["title"] and item["source"] == "manual":
            updated = queries.update_item_title(item["id"], title) or updated
        changed += updated
    return changed
