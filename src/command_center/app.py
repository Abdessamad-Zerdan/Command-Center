import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from command_center import auth, fixtures, pipeline, queries, setup_wizard
from command_center.assistant import ingest as assistant_ingest
from command_center.assistant.router import router as assistant_router
from command_center.finances.router import router as finances_router
from command_center.history.router import router as history_router
from command_center.projects.router import router as projects_router
from command_center.scheduling.router import router as scheduling_router
from command_center.config import (
    BUILD_LOG,
    FOOTER_LINKS,
    LANE_LABELS,
    LANES,
    PROFILE,
    PROJECTS,
    REPO_ROOT,
    TZ,
)
from command_center.db import init_db

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
STATIC_DIR = REPO_ROOT / "static"
STATIC_DIR.mkdir(exist_ok=True)
(STATIC_DIR / "img").mkdir(exist_ok=True)

# One recurring coordinator tick rather than four independently-scheduled
# jobs — changing a source's interval in Settings just changes what the
# next tick reads, no job re-registration needed.
COORDINATOR_INTERVAL_MINUTES = 5
_scheduler: BackgroundScheduler | None = None


def _coordinator_tick() -> None:
    now = datetime.now(TZ)
    for cfg in queries.list_source_configs():
        if not cfg["enabled"]:
            continue
        last_pulled = cfg["last_pulled_at"]
        due = last_pulled is None
        if last_pulled:
            elapsed_minutes = (now - datetime.fromisoformat(last_pulled)).total_seconds() / 60
            due = elapsed_minutes >= cfg["interval_minutes"]
        if not due:
            continue
        try:
            pipeline.run_source(cfg["source_name"])
        except Exception:
            logger.exception("Scheduled pull failed for source %s", cfg["source_name"])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _scheduler
    init_db()
    pipeline.seed_source_config()  # last_pulled_at seeded to now — no pull-on-startup
    queries.seed_app_settings()  # day-bounds defaults, never clobbers existing values
    try:
        assistant_ingest.rebuild_index()  # no-op if data/vision.md is unchanged
    except Exception:
        # First-run model download can fail offline; degrade rather than
        # block startup — same "never fail the whole brief" philosophy
        # as every other source in pipeline.py.
        logger.exception("Assistant index build failed at startup")
    if auth.has_valid_credentials():
        if queries.get_brief(_today()) is None:
            try:
                pipeline.run()
            except Exception:
                logger.exception("Startup pipeline run failed")
        _scheduler = BackgroundScheduler(timezone=TZ)
        _scheduler.add_job(
            _coordinator_tick,
            "interval",
            minutes=COORDINATOR_INTERVAL_MINUTES,
            id="source_coordinator",
        )
        _scheduler.start()
    else:
        fixtures.seed()  # pre-`make auth` demo mode
    yield
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="Daily Command Center", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
setup_wizard.register(app)  # remove this line (+ the setup_wizard package) to remove the wizard
app.include_router(assistant_router)
app.include_router(finances_router)
app.include_router(history_router)  # before any @app.get("/history/{brief_date}") is registered below
app.include_router(scheduling_router)
app.include_router(projects_router)

# Timeline strip window — business hours are what fit on a phone without scrolling.
TIMELINE_START_HOUR = 8
TIMELINE_END_HOUR = 20


def _timeline_pct(dt: datetime) -> float:
    window_minutes = (TIMELINE_END_HOUR - TIMELINE_START_HOUR) * 60
    minutes_in = (dt.hour - TIMELINE_START_HOUR) * 60 + dt.minute
    return max(0.0, min(100.0, (minutes_in / window_minutes) * 100))


def _position_events(events: list[dict]) -> list[dict]:
    positioned = []
    for ev in events:
        start = datetime.fromisoformat(ev["start_time"])
        end = datetime.fromisoformat(ev["end_time"])
        left = _timeline_pct(start)
        width = max(2.0, _timeline_pct(end) - left)
        positioned.append({**ev, "left_pct": left, "width_pct": width})
    return positioned


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _lane_counts(lanes: dict[str, list[dict]]) -> dict[str, int]:
    return {
        lane: sum(1 for item in items if item["status"] == "pending")
        for lane, items in lanes.items()
    }


@app.get("/")
def home(request: Request):
    brief = queries.get_brief(_today())
    tasks_count = sum(len(items) for items in brief["lanes"].values()) if brief else 0
    due_today_count = len(brief["lanes"]["tasks_due"]) if brief else 0

    return templates.TemplateResponse(
        request,
        "portfolio.html",
        {
            "profile": PROFILE,
            "projects": PROJECTS,
            "footer_links": FOOTER_LINKS,
            "build_log": BUILD_LOG,
            "brief_date_short": datetime.now(TZ).strftime("%b %d"),
            "tasks_count": tasks_count,
            "due_today_count": due_today_count,
            "pomodoro_history": queries.list_pomodoro_history(),
            "time_by_task": queries.get_time_by_task(),
        },
    )


@app.get("/brief")
def dashboard(request: Request):
    brief_date = _today()
    brief = queries.get_brief(brief_date)
    if brief is None:
        # Not an error from the visitor's perspective — same "show an
        # explanatory empty state, not an exception" posture every other
        # page in this app already uses for "no data yet." Realistic
        # cause: the app just started and the first pipeline.run() either
        # hasn't finished or failed (lifespan logs and degrades rather
        # than crashing startup) — fixtures.seed()'s demo-mode branch
        # already guarantees a row exists whenever Google isn't
        # connected, so this is specifically the "Google is connected but
        # the first real pull hasn't landed yet" case.
        return templates.TemplateResponse(request, "brief_not_ready.html", {})

    now = datetime.now(TZ)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "brief": brief,
            "lane_labels": LANE_LABELS,
            "lanes_order": LANES,
            "lane_counts": _lane_counts(brief["lanes"]),
            "now_istanbul": now,
            "now_pct": _timeline_pct(now),
            "timeline_start_hour": TIMELINE_START_HOUR,
            "timeline_end_hour": TIMELINE_END_HOUR,
            "events": _position_events(brief["events"]),
            "is_history": False,
            "tz_name": str(TZ),
            "active_projects": queries.list_registered_projects(active_only=True),
        },
    )


@app.get("/history")
def history_list(request: Request):
    dates = queries.list_history_dates()
    return templates.TemplateResponse(
        request, "history.html", {"dates": dates, "today": _today()}
    )


@app.get("/history/{brief_date}")
def history_detail(request: Request, brief_date: str):
    brief = queries.get_brief(brief_date)
    if brief is None:
        raise HTTPException(status_code=404, detail="No brief for that date")

    now = datetime.now(TZ)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "brief": brief,
            "lane_labels": LANE_LABELS,
            "lanes_order": LANES,
            "lane_counts": _lane_counts(brief["lanes"]),
            "now_istanbul": now,
            "now_pct": _timeline_pct(now),
            "timeline_start_hour": TIMELINE_START_HOUR,
            "timeline_end_hour": TIMELINE_END_HOUR,
            "events": _position_events(brief["events"]),
            "is_history": brief_date != _today(),
            "tz_name": str(TZ),
            "active_projects": queries.list_registered_projects(active_only=True),
        },
    )


@app.get("/brief/lanes-partial")
def lanes_partial(request: Request, date: str | None = None):
    """Re-renders just the lane grid for `date` (defaults to today) as
    {html, lane_counts} JSON — lets the assistant widget catch up the
    currently-viewed page after a chat-driven create/update/complete/
    move without a full reload, which would otherwise drop the visible
    conversation (never persisted past the tab's memory in the first
    place). Every other in-page mutation (drag-drop, inline add, title
    edit) still just reloads — this route exists specifically for the
    chat path, where preserving the conversation actually matters.
    """
    brief_date = date or _today()
    brief = queries.get_brief(brief_date)
    if brief is None:
        raise HTTPException(status_code=404, detail="No brief for that date")

    html = templates.env.get_template("partials/lanes.html").render(
        request=request,
        brief=brief,
        lane_labels=LANE_LABELS,
        lanes_order=LANES,
        is_history=brief_date != _today(),
    )
    return JSONResponse({"html": html, "lane_counts": _lane_counts(brief["lanes"])})


@app.post("/items/{item_id}/done")
def mark_done(item_id: int):
    queries.set_item_status(item_id, "done")
    return {"ok": True}


@app.post("/items/{item_id}/snooze")
def mark_snoozed(item_id: int):
    queries.set_item_status(item_id, "snoozed")
    return {"ok": True}


@app.post("/items/{item_id}/move-to-today")
def move_item_to_today(item_id: int):
    moved = queries.move_item_to_date(item_id, _today())
    if not moved:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@app.post("/rerun")
def rerun():
    if auth.has_valid_credentials():
        try:
            pipeline.run()
        except Exception:
            logger.exception("Manual rerun failed")
    else:
        # Fixture mode: no credentials yet, just bump the timestamp so the
        # "last run" indicator visibly responds to the button.
        queries.touch_brief_generated_at(_today())
    return RedirectResponse(url="/brief", status_code=303)


class PomodoroSessionIn(BaseModel):
    task_name: str
    planned_minutes: int
    elapsed_seconds: int
    status: str  # "completed" | "stopped_early"


@app.post("/pomodoro/sessions")
def create_pomodoro_session(payload: PomodoroSessionIn):
    ended_at = datetime.now(TZ)
    started_at = ended_at - timedelta(seconds=payload.elapsed_seconds)
    session_id = queries.create_pomodoro_session(
        task_name=payload.task_name,
        planned_minutes=payload.planned_minutes,
        elapsed_seconds=payload.elapsed_seconds,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        status=payload.status,
    )
    return {"id": session_id}


class ManualItemIn(BaseModel):
    lane: str
    title: str
    due_date: str | None = None


@app.post("/items")
def create_item(payload: ManualItemIn):
    if payload.lane not in LANES:
        raise HTTPException(status_code=400, detail=f"Unknown lane: {payload.lane!r}")
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="Title can't be empty")
    due_date = payload.due_date or None
    item_id = queries.create_manual_item(_today(), payload.lane, payload.title.strip(), due_date=due_date)
    return {"id": item_id}


class ItemTitleIn(BaseModel):
    title: str


@app.patch("/items/{item_id}/title")
def update_item_title(item_id: int, payload: ItemTitleIn):
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="Title can't be empty")
    updated = queries.update_item_title(item_id, payload.title.strip())
    if not updated:
        raise HTTPException(
            status_code=403,
            detail="Only manually-added tasks can be edited",
        )
    return {"ok": True}


class ItemLaneIn(BaseModel):
    lane: str


@app.patch("/items/{item_id}/lane")
def update_item_lane(item_id: int, payload: ItemLaneIn):
    if payload.lane not in LANES:
        raise HTTPException(status_code=400, detail=f"Unknown lane: {payload.lane!r}")
    updated = queries.update_item_lane(item_id, payload.lane)
    if not updated:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


class ItemProjectIn(BaseModel):
    project_id: int | None = None


@app.patch("/items/{item_id}/project")
def update_item_project(item_id: int, payload: ItemProjectIn):
    updated = queries.update_item_project(item_id, payload.project_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@app.delete("/pomodoro/sessions/{session_id}")
def delete_pomodoro_session(session_id: int):
    deleted = queries.delete_pomodoro_session(session_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return deleted


class RenameTaskIn(BaseModel):
    old_name: str
    new_name: str


@app.patch("/pomodoro/tasks")
def rename_pomodoro_task(payload: RenameTaskIn):
    if not payload.new_name.strip():
        raise HTTPException(status_code=400, detail="Name can't be empty")
    count = queries.rename_pomodoro_task(payload.old_name, payload.new_name.strip())
    return {"renamed": count}


@app.get("/settings")
def settings_page(request: Request):
    index_status = assistant_ingest.get_index_status()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "sources": queries.list_source_configs(),
            "assistant_last_indexed_at": index_status["last_indexed_at"],
            "assistant_chunk_count": index_status["chunk_count"],
        },
    )


class SourceConfigIn(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None


@app.patch("/settings/sources/{source_name}")
def update_source_config(source_name: str, payload: SourceConfigIn):
    if source_name not in pipeline.SOURCE_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_name!r}")
    queries.update_source_config(
        source_name, enabled=payload.enabled, interval_minutes=payload.interval_minutes
    )
    return {"ok": True}


@app.post("/settings/sources/{source_name}/pull")
def pull_source_now(source_name: str):
    if source_name not in pipeline.SOURCE_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_name!r}")
    # Medium needs no Google auth; gmail/calendar/tasks do — same gate /rerun
    # uses, so a pre-`make auth` click degrades instead of raising.
    if source_name != "medium" and not auth.has_valid_credentials():
        logger.info("Skipping pull for %s: no Google credentials yet", source_name)
        return RedirectResponse(url="/settings", status_code=303)
    try:
        pipeline.run_source(source_name)
    except Exception:
        logger.exception("Manual pull failed for source %s", source_name)
    return RedirectResponse(url="/settings", status_code=303)
