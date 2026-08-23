"""GET /fitness — a hidden (unlinked, direct-URL-only) personal training
+ nutrition tracker. Reachable only by typing the URL: it is deliberately
absent from base.html's nav_items and commandPalette() route list.

This page is a mirror of hand-entered data, never a coach: every target
compared against here (protein grams, session counts, the running cap)
lives in fitness_settings and is edited by the user at /fitness/settings,
never computed or suggested by this code. All status chips are additive
and neutral — see fitness/aggregations.py's own docstring.
"""

import csv
import io
import sqlite3
from datetime import date as date_type
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from command_center import queries
from command_center.config import TZ
from command_center.fitness import aggregations, charts

router = APIRouter()

# A fresh Jinja2Templates instance, same reasoning as every other feature
# router in this app: app.py imports this router, so importing app.py's
# own `templates` back here would be circular.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

RUNNING_INTENSITIES = ("easy", "moderate", "hard")
CHART_WEEKS = 8


def _today() -> date_type:
    return datetime.now(TZ).date()


def _month_bounds(today: date_type) -> tuple[str, str]:
    """[start, end) half-open ISO strings for today's own calendar
    month — the current month is what "this month's data" means with
    no month picker on the page (yet) to say otherwise."""
    start = today.replace(day=1)
    end = date_type(start.year + 1, 1, 1) if start.month == 12 else date_type(start.year, start.month + 1, 1)
    return start.isoformat(), end.isoformat()


def _rows_to_csv(rows: list[dict]) -> str:
    """Empty table -> empty string (no header row), same as
    app.py's own _rows_to_csv — an empty CSV download is a legitimate,
    unsurprising result for "nothing logged this month yet.\""""
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


@router.get("/fitness/export/daily-log.csv")
def export_daily_log_csv():
    start, end = _month_bounds(_today())
    rows = [
        {
            "log_date": log["log_date"],
            "bodyweight_kg": log["bodyweight_kg"],
            "protein_g": log["protein_g"],
            "addons_checked": ", ".join(log["addons_checked"]),
            "note": log["note"],
        }
        for log in queries.list_daily_logs(start, end)
    ]
    return Response(
        content=_rows_to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=fitness-daily-log-{start[:7]}.csv"},
    )


@router.get("/fitness/export/training.csv")
def export_training_csv():
    """One row per exercise (not per session) — a session's own volume
    is just sum(sets * reps * weight_kg) across its rows, so nothing is
    lost by flattening, and a flat sheet is what's actually usable in a
    spreadsheet."""
    start, end = _month_bounds(_today())
    rows = [
        {
            "session_date": s["session_date"],
            "exercise": ex["name"],
            "sets": ex["sets"],
            "reps": ex["reps"],
            "weight_kg": ex["weight_kg"],
            "volume_kg": ex["sets"] * ex["reps"] * ex["weight_kg"],
        }
        for s in queries.list_training_sessions(start, end)
        for ex in s["exercises"]
    ]
    return Response(
        content=_rows_to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=fitness-training-{start[:7]}.csv"},
    )


@router.get("/fitness/export/running.csv")
def export_running_csv():
    start, end = _month_bounds(_today())
    rows = [
        {"run_date": r["run_date"], "distance_km": r["distance_km"], "intensity": r["intensity"]}
        for r in queries.list_running_logs(start, end)
    ]
    return Response(
        content=_rows_to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=fitness-running-{start[:7]}.csv"},
    )


@router.get("/fitness")
def fitness_dashboard(request: Request):
    settings = queries.get_fitness_settings()
    today = _today()
    today_iso = today.isoformat()
    week_start, week_end = aggregations.trailing_window(today, 7)

    weight_points = queries.list_bodyweight_log()
    volume_series = aggregations.weekly_volume_series(today, CHART_WEEKS)
    running_series = aggregations.weekly_running_series(today, CHART_WEEKS)

    return templates.TemplateResponse(
        request,
        "fitness_dashboard.html",
        {
            "settings": settings,
            "today": today_iso,
            "todays_log": queries.get_daily_log(today_iso),
            "addons": queries.list_surplus_addons(),
            "protein": aggregations.protein_status(today, settings["protein_target_g_per_day"]),
            "surplus": aggregations.surplus_addon_status(today),
            "volume": aggregations.training_volume_status(today),
            "running": aggregations.running_status(today, settings["running_weekly_distance_cap_km"]),
            "stats": aggregations.headline_stats(today, settings),
            "weight_trend_svg": charts.render_weight_trend_svg(
                weight_points, settings["goal_weight_low"], settings["goal_weight_high"]
            ),
            "volume_bars_svg": charts.render_bars_svg(volume_series),
            "running_bars_svg": charts.render_bars_svg(
                running_series, cap_value=settings["running_weekly_distance_cap_km"]
            ),
            "recent_sessions": queries.list_training_sessions(week_start, week_end),
            "recent_runs": queries.list_running_logs(week_start, week_end),
        },
    )


# --- settings -------------------------------------------------------------


@router.get("/fitness/settings")
def fitness_settings_page(request: Request):
    return templates.TemplateResponse(
        request,
        "fitness_settings.html",
        {"settings": queries.get_fitness_settings(), "addons": queries.list_surplus_addons()},
    )


class FitnessSettingsIn(BaseModel):
    goal_weight_low: float
    goal_weight_high: float
    height_cm: float
    age: int
    current_weight: float
    protein_target_g_per_day: float
    resistance_sessions_per_week_target: int
    running_sessions_per_week_cap: int
    running_weekly_distance_cap_km: float | None = None


@router.patch("/fitness/settings")
def update_fitness_settings(payload: FitnessSettingsIn):
    if payload.goal_weight_low > payload.goal_weight_high:
        raise HTTPException(status_code=400, detail="Goal weight low must not exceed goal weight high.")
    queries.update_fitness_settings(**payload.model_dump())
    return {"ok": True}


class SurplusAddonIn(BaseModel):
    name: str


@router.post("/fitness/settings/addons")
def add_surplus_addon(payload: SurplusAddonIn):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Enter a name.")
    try:
        addon_id = queries.create_surplus_addon(name)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Already in your list.") from None
    return {"ok": True, "id": addon_id}


@router.delete("/fitness/settings/addons/{addon_id}")
def remove_surplus_addon(addon_id: int):
    if not queries.delete_surplus_addon(addon_id):
        raise HTTPException(status_code=404, detail="Add-on not found")
    return {"ok": True}


# --- daily log --------------------------------------------------------------


class DailyLogIn(BaseModel):
    log_date: str
    bodyweight_kg: float | None = None
    protein_g: float | None = None
    addons_checked: list[str] = []
    note: str = ""


@router.post("/fitness/daily-log")
def save_daily_log(payload: DailyLogIn):
    try:
        date_type.fromisoformat(payload.log_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date: {payload.log_date!r}") from exc

    queries.upsert_daily_log(
        payload.log_date,
        payload.bodyweight_kg,
        payload.protein_g,
        payload.addons_checked,
        payload.note.strip(),
    )
    # Keeps the headline "current weight" stat in step with same-day
    # entries without a separate Settings edit — the point of this
    # tracker is a 20-second daily habit, not two places to update the
    # same number. Only for *today*'s entry: backfilling a past day's
    # weight must never overwrite a more recent reading.
    if payload.bodyweight_kg is not None and payload.log_date == _today().isoformat():
        queries.set_current_weight(payload.bodyweight_kg)
    return {"ok": True}


# --- training sessions --------------------------------------------------------


class ExerciseIn(BaseModel):
    name: str
    sets: int
    reps: int
    weight_kg: float


class TrainingSessionIn(BaseModel):
    session_date: str
    exercises: list[ExerciseIn]


@router.post("/fitness/training-sessions")
def save_training_session(payload: TrainingSessionIn):
    try:
        date_type.fromisoformat(payload.session_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date: {payload.session_date!r}") from exc
    exercises = [e.model_dump() for e in payload.exercises if e.name.strip()]
    if not exercises:
        raise HTTPException(status_code=400, detail="Add at least one exercise.")

    session_id = queries.create_training_session(payload.session_date, exercises)
    return {"ok": True, "id": session_id}


@router.delete("/fitness/training-sessions/{session_id}")
def remove_training_session(session_id: int):
    if not queries.delete_training_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


# --- running log --------------------------------------------------------------


class RunningLogIn(BaseModel):
    run_date: str
    distance_km: float
    intensity: str


@router.post("/fitness/running-log")
def save_running_log(payload: RunningLogIn):
    try:
        date_type.fromisoformat(payload.run_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date: {payload.run_date!r}") from exc
    if payload.intensity not in RUNNING_INTENSITIES:
        raise HTTPException(status_code=400, detail=f"Unknown intensity: {payload.intensity!r}")
    if payload.distance_km <= 0:
        raise HTTPException(status_code=400, detail="Distance must be positive")

    run_id = queries.create_running_log(payload.run_date, payload.distance_km, payload.intensity)
    return {"ok": True, "id": run_id}


@router.delete("/fitness/running-log/{run_id}")
def remove_running_log(run_id: int):
    if not queries.delete_running_log(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return {"ok": True}
