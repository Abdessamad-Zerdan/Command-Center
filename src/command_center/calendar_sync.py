"""Shared "push one item to Google Calendar" logic. Used by both
POST /items/{item_id}/add-to-calendar (app.py) and POST /calendar/tasks
(calendar_view/router.py, which creates a manual task and syncs it to
Google in the same request). Lives in its own module specifically so
neither of those two routers has to import the other — app.py already
imports calendar_view.router, so the reverse would be circular.
"""

from datetime import datetime, timedelta

from fastapi import HTTPException
from googleapiclient.errors import HttpError

from command_center import auth, queries
from command_center.config import TZ
from command_center.sources.calendar import CalendarSource


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
    return {"link": link}
