"""GET/POST/PATCH/DELETE for /settings/schedule (recurring_commitments +
day-bounds CRUD) and GET /schedule (the read-only weekly free-time view).
Own APIRouter + own Jinja2Templates instance, same reasoning as
history/router.py: app.py must import this router, so this file can't
import app.py's templates instance back without a circular import.

Route collision check: /settings/schedule* shares no path prefix with
the existing /settings/sources/{source_name} routes (different literal
first segment), and /schedule (top-level) has no path-param sibling
route to be shadowed by or shadow — unlike /history/report vs
/history/{brief_date}, registration order doesn't matter here.
"""

from datetime import date as date_type
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from command_center import queries
from command_center.config import TZ
from command_center.history import periods
from command_center.scheduling import availability, suggest

router = APIRouter()
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _today() -> date_type:
    return datetime.now(TZ).date()


def _validate_time_range(start: str, end: str) -> None:
    try:
        start_t = datetime.strptime(start, "%H:%M").time()
        end_t = datetime.strptime(end, "%H:%M").time()
    except ValueError:
        raise HTTPException(status_code=400, detail="Times must be HH:MM")
    if end_t <= start_t:
        raise HTTPException(status_code=400, detail="end_time must be after start_time")


@router.get("/settings/schedule")
def schedule_settings_page(request: Request):
    day_bounds_start, day_bounds_end = queries.get_day_bounds()
    return templates.TemplateResponse(
        request,
        "settings_schedule.html",
        {
            "commitments": queries.list_recurring_commitments(),
            "day_names": _DAY_NAMES,
            "day_bounds_start": day_bounds_start,
            "day_bounds_end": day_bounds_end,
        },
    )


class RecurringCommitmentIn(BaseModel):
    title: str
    day_of_week: int
    start_time: str
    end_time: str


@router.post("/settings/schedule/commitments")
def create_commitment(payload: RecurringCommitmentIn):
    if not (0 <= payload.day_of_week <= 6):
        raise HTTPException(status_code=400, detail="day_of_week must be 0-6")
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="Title can't be empty")
    _validate_time_range(payload.start_time, payload.end_time)
    commitment_id = queries.create_recurring_commitment(
        payload.title.strip(), payload.day_of_week, payload.start_time, payload.end_time
    )
    return {"id": commitment_id}


class RecurringCommitmentPatch(BaseModel):
    title: str | None = None
    day_of_week: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    active: bool | None = None


@router.patch("/settings/schedule/commitments/{commitment_id}")
def patch_commitment(commitment_id: int, payload: RecurringCommitmentPatch):
    if payload.day_of_week is not None and not (0 <= payload.day_of_week <= 6):
        raise HTTPException(status_code=400, detail="day_of_week must be 0-6")
    if payload.title is not None and not payload.title.strip():
        raise HTTPException(status_code=400, detail="Title can't be empty")
    updated = queries.update_recurring_commitment(
        commitment_id,
        title=payload.title.strip() if payload.title is not None else None,
        day_of_week=payload.day_of_week,
        start_time=payload.start_time,
        end_time=payload.end_time,
        active=payload.active,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Commitment not found")
    return {"ok": True}


@router.delete("/settings/schedule/commitments/{commitment_id}")
def delete_commitment(commitment_id: int):
    deleted = queries.delete_recurring_commitment(commitment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Commitment not found")
    return {"ok": True}


class DayBoundsIn(BaseModel):
    day_bounds_start: str
    day_bounds_end: str


@router.patch("/settings/schedule/day-bounds")
def update_day_bounds(payload: DayBoundsIn):
    _validate_time_range(payload.day_bounds_start, payload.day_bounds_end)
    queries.set_day_bounds(payload.day_bounds_start, payload.day_bounds_end)
    return {"ok": True}


@router.get("/schedule")
def schedule_page(request: Request):
    today = _today()
    week_start, week_end = periods.period_bounds("week", today)
    week = availability.free_slots_for_week(week_start)
    days = [
        {
            "date": d,
            "date_label": f"{d.strftime('%b')} {d.day}",
            "label": _DAY_NAMES[d.weekday()],
            "is_today": d == today,
            "free_slots": slots,
            "calendar_checked": availability.calendar_checked(d),
        }
        for d, slots in sorted(week.items())
    ]
    return templates.TemplateResponse(
        request,
        "schedule.html",
        {
            "days": days,
            "week_label": periods.label("week", week_start, week_end),
        },
    )


@router.post("/schedule/suggest")
def suggest_schedule():
    week_start, _ = periods.period_bounds("week", _today())
    return {"placements": suggest.suggest_placements(week_start)}


class AcceptPlacementIn(BaseModel):
    item_id: int
    scheduled_date: str  # YYYY-MM-DD
    scheduled_start: str  # HH:MM
    scheduled_end: str  # HH:MM


@router.post("/schedule/accept-placement")
def accept_placement(payload: AcceptPlacementIn):
    try:
        start_dt = datetime.strptime(f"{payload.scheduled_date} {payload.scheduled_start}", "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(f"{payload.scheduled_date} {payload.scheduled_end}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise HTTPException(status_code=400, detail="scheduled_date must be YYYY-MM-DD and times HH:MM")
    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="scheduled_end must be after scheduled_start")

    updated = queries.schedule_item(payload.item_id, start_dt.isoformat(), end_dt.isoformat())
    if not updated:
        raise HTTPException(status_code=404, detail="Item not found or not pending")

    queries.log_tool_call(
        "schedule_task",
        {
            "item_id": payload.item_id,
            "scheduled_start": start_dt.isoformat(),
            "scheduled_end": end_dt.isoformat(),
        },
        "confirmed",
    )
    return {"ok": True}
