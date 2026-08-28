"""GET /calendar — a month-grid view combining live Google Calendar
events with every pending task's due date, so "what's coming" is
visible without leaving the app. Own APIRouter + own Jinja2Templates
instance, same reasoning as every other feature router in this app:
app.py imports this router, so importing app.py's own templates back
here would be circular.

Moving a task between dates (drag-and-drop, or the "Today" shortcut)
goes through the generic PATCH /items/{item_id}/due-date in app.py —
same "item mutations live in app.py, feature pages live in their own
router" split every other /items/{id}/... route already follows.
POST /calendar/tasks (below) is the one exception, since it's not a
generic item mutation — it's specific to this page (a per-day quick-add
that also pushes the new task straight to Google Calendar).
"""

import logging
from datetime import date as date_type
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from googleapiclient.errors import HttpError
from pydantic import BaseModel

from command_center import auth, calendar_sync, queries
from command_center.calendar_view import grid
from command_center.config import LANES, TZ
from command_center.sources.calendar import CalendarSource

logger = logging.getLogger(__name__)

router = APIRouter()
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _today() -> date_type:
    return datetime.now(TZ).date()


@router.get("/calendar")
def calendar_month_view(request: Request, month: str | None = None):
    if month:
        try:
            month_start = date_type.fromisoformat(f"{month}-01")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid month: {month!r}") from None
    else:
        month_start = _today().replace(day=1)

    weeks = grid.build_weeks(month_start)
    grid_start = weeks[0][0]
    grid_end = weeks[-1][-1] + timedelta(days=1)  # weeks[-1][-1] is inclusive; queries want [start, end)

    events_by_date: dict[str, list[dict]] = {}
    calendar_connected = auth.has_valid_credentials()
    calendar_degraded = False
    if calendar_connected:
        try:
            credentials = auth.get_google_credentials()
            for ev in CalendarSource(credentials).fetch_month_events(grid_start, grid_end):
                events_by_date.setdefault(ev["date"], []).append(ev)
        except (auth.AuthNotConfigured, HttpError):
            logger.exception("Fetching month calendar events failed")
            calendar_degraded = True

    tasks_by_date: dict[str, list[dict]] = {}
    for item in queries.list_pending_items_by_due_date(grid_start.isoformat(), grid_end.isoformat()):
        tasks_by_date.setdefault(item["due_date"], []).append(item)

    today_iso = _today().isoformat()
    week_rows = [
        [
            {
                "date": d.isoformat(),
                "day": d.day,
                "in_month": d.month == month_start.month,
                "is_today": d.isoformat() == today_iso,
                "events": events_by_date.get(d.isoformat(), []),
                "tasks": tasks_by_date.get(d.isoformat(), []),
            }
            for d in week
        ]
        for week in weeks
    ]

    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "weeks": week_rows,
            "month_label": month_start.strftime("%B %Y"),
            "prev_month": grid.shift_month(month_start, -1).strftime("%Y-%m"),
            "next_month": grid.shift_month(month_start, 1).strftime("%Y-%m"),
            "current_month": month_start.strftime("%Y-%m"),
            "today": today_iso,
            "calendar_connected": calendar_connected,
            "calendar_degraded": calendar_degraded,
            "lanes_order": LANES,
            "lane_labels": queries.get_lane_labels(),
        },
    )


class QuickTaskIn(BaseModel):
    lane: str
    title: str
    due_date: str


@router.post("/calendar/tasks")
def quick_add_task(payload: QuickTaskIn):
    """Manually set a task straight from the month grid, syncing it to
    Google Calendar in the same request. The task itself is saved
    either way — a Google sync failure (not connected, API hiccup) is
    reported back as calendar_synced=False rather than rolling back the
    task, since losing a task the user just typed over a transient
    Google error would be worse than a task that's momentarily
    calendar-less. (See calendar_sync.sync_item_to_calendar's own
    docstring for what a "failure" here actually maps to.)
    """
    if payload.lane not in LANES:
        raise HTTPException(status_code=400, detail=f"Unknown lane: {payload.lane!r}")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title can't be empty")
    try:
        date_type.fromisoformat(payload.due_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date: {payload.due_date!r}") from exc

    item_id = queries.create_manual_item(_today().isoformat(), payload.lane, title, due_date=payload.due_date)

    try:
        result = calendar_sync.sync_item_to_calendar(queries.get_item_for_calendar(item_id))
    except HTTPException as exc:
        return {"ok": True, "id": item_id, "calendar_synced": False, "calendar_error": exc.detail}

    return {"ok": True, "id": item_id, "calendar_synced": True, "link": result["link"]}
