import csv
import io
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from command_center import auth, calendar_sync, fixtures, notify, nudges, pipeline, queries, setup_wizard, triage_rules
from command_center.assistant import ingest as assistant_ingest
from command_center.assistant.router import router as assistant_router
from command_center.calendar_view.router import router as calendar_router
from command_center.finances.router import router as finances_router
from command_center.fitness.router import router as fitness_router
from command_center.history.router import router as history_router
from command_center.projects.router import router as projects_router
from command_center.scheduling.router import router as scheduling_router
from command_center.config import (
    BUILD_LOG,
    FOOTER_LINKS,
    LANES,
    PROFILE,
    PROJECTS,
    REPO_ROOT,
    TRIAGE_PROVIDER,
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


_NUDGE_PRIORITY = {"stale_urgent": "high", "overdue_tasks": "high", "no_pull_today": "default"}


def _notify_new_nudges() -> None:
    """Pushes any currently-active nudge (see nudges.py) via ntfy that
    hasn't already been sent today — a no-op entirely if NTFY_TOPIC
    isn't set. Runs on the same 5-minute tick as the source pulls, not
    on page load, so it fires once in the background even if nobody
    opens the brief that day; was_nudge_notified_today's per-id,
    per-day dedup is what keeps a still-unresolved nudge from spamming
    every tick.
    """
    if not notify.is_configured():
        return
    today = datetime.now(TZ).date().isoformat()
    for nudge in nudges.compute_nudges(today):
        if queries.was_nudge_notified_today(nudge["id"], today):
            continue
        sent = notify.send(
            "Daily Command Center",
            nudge["message"],
            priority=_NUDGE_PRIORITY.get(nudge["id"], "default"),
        )
        if sent:
            queries.mark_nudge_notified(nudge["id"], today)


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
    try:
        _notify_new_nudges()
    except Exception:
        logger.exception("Nudge notification check failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _scheduler
    init_db()
    pipeline.seed_source_config()  # last_pulled_at seeded to now — no pull-on-startup
    queries.seed_app_settings()  # day-bounds defaults, never clobbers existing values
    queries.seed_fitness_settings()  # /fitness defaults + starter surplus add-ons
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
app.include_router(calendar_router)
app.include_router(finances_router)
app.include_router(fitness_router)
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


def _format_duration(total_seconds: int) -> str:
    """Xm under an hour, Xh Ym (no trailing '0m') at or above — same
    convention the Pomodoro widget's own client-side formatDuration()
    already uses, kept consistent rather than introducing a second
    duration format in the same app."""
    mins = round(total_seconds / 60)
    if mins < 60:
        return f"{mins}m"
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _annotate_time_logged(lanes: dict[str, list[dict]]) -> None:
    """Mutates each item in-place with time_logged_seconds (0 if
    nothing's been logged, not just absent) and a pre-formatted
    time_logged_display for the "Time logged: 2h 30m" badge — total
    Pomodoro time ever logged against it, across every lane."""
    all_items = [item for items in lanes.values() for item in items]
    totals = queries.get_time_logged_by_source_ids([item["id"] for item in all_items])
    for item in all_items:
        seconds = totals.get(item["id"], 0)
        item["time_logged_seconds"] = seconds
        item["time_logged_display"] = _format_duration(seconds) if seconds else None


def _health_snapshot() -> dict:
    """Structural, no-live-network snapshot — same philosophy as
    setup_wizard/status.py's is_setup_complete(): reports what's
    configured/what the last attempt recorded, not a fresh live probe of
    every external service on each call. degraded_today reads straight
    off today's briefs.degraded_lanes, the same field the brief page's
    own banner already surfaces — this just makes it visible from
    Settings too, and in a form a monitoring tool can poll.
    """
    today_brief = queries.get_brief(_today())
    degraded_today = today_brief["degraded_lanes"] if today_brief else []
    return {
        "status": "ok" if not degraded_today else "degraded",
        "google_connected": auth.has_valid_credentials(),
        "triage_provider": TRIAGE_PROVIDER,
        "degraded_today": degraded_today,
        "sources": queries.list_source_configs(),
        "assistant_index": assistant_ingest.get_index_status(),
    }


@app.get("/health")
def health():
    return JSONResponse(_health_snapshot())


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

    _annotate_time_logged(brief["lanes"])
    now = datetime.now(TZ)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "brief": brief,
            "lane_labels": queries.get_lane_labels(),
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
            "nudges": nudges.compute_nudges(brief_date),
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

    _annotate_time_logged(brief["lanes"])
    now = datetime.now(TZ)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "brief": brief,
            "lane_labels": queries.get_lane_labels(),
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
            "nudges": [],
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

    _annotate_time_logged(brief["lanes"])
    html = templates.env.get_template("partials/lanes.html").render(
        request=request,
        brief=brief,
        lane_labels=queries.get_lane_labels(),
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


@app.post("/items/{item_id}/dismiss")
def dismiss_item(item_id: int):
    # Same dedup guarantee as done/snoozed: get_brief() only ever selects
    # status = 'pending', and save_triage_results() dedups new pulls on
    # the item's (source, source_id) via INSERT OR IGNORE — so a
    # dismissed item's row is never re-inserted as 'pending' by a later
    # triage run, meaning it never resurfaces on a future brief.
    queries.set_item_status(item_id, "dismissed")
    return {"ok": True}


@app.post("/items/{item_id}/move-to-today")
def move_item_to_today(item_id: int):
    moved = queries.move_item_to_date(item_id, _today())
    if not moved:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


class MoveToDateIn(BaseModel):
    date: str


@app.post("/items/{item_id}/move-to-date")
def move_item_to_date_route(item_id: int, payload: MoveToDateIn):
    # The generic form of move-to-today — used by the undo toast to move
    # an item back to whichever date it actually came from, not always
    # today. move-to-today itself stays a separate, no-body route since
    # that's the one real users click.
    moved = queries.move_item_to_date(item_id, payload.date)
    if not moved:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@app.post("/items/{item_id}/reopen")
def reopen_item(item_id: int):
    # Undo for snooze/done/dismiss — sets status back to 'pending' without
    # touching brief_date/lane. No caller did this before the undo
    # toast; set_item_status has always supported it (see its own
    # _STATUS_EVENT_MAP, logged as "reopened").
    queries.set_item_status(item_id, "pending")
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


class PomodoroStartIn(BaseModel):
    task_name: str
    planned_minutes: int
    task_source_id: int | None = None


@app.post("/pomodoro/sessions")
def start_pomodoro_session(payload: PomodoroStartIn):
    if not payload.task_name.strip():
        raise HTTPException(status_code=400, detail="Task name can't be empty")
    if payload.planned_minutes <= 0:
        raise HTTPException(status_code=400, detail="Planned duration must be positive")
    session_id = queries.start_pomodoro_session(
        task_name=payload.task_name.strip(),
        planned_minutes=payload.planned_minutes,
        task_source_id=payload.task_source_id,
    )
    return {"id": session_id}


class PomodoroHeartbeatIn(BaseModel):
    elapsed_seconds: int
    paused: bool = False
    resumed: bool = False


@app.patch("/pomodoro/sessions/{session_id}/heartbeat")
def heartbeat_pomodoro_session(session_id: int, payload: PomodoroHeartbeatIn):
    now = datetime.now(TZ).isoformat()
    updated = queries.update_pomodoro_heartbeat(
        session_id,
        elapsed_seconds=max(0, payload.elapsed_seconds),
        paused_at=now if payload.paused else None,
        resumed_at=now if payload.resumed else None,
    )
    if not updated:
        # Not an error the widget should surface — a heartbeat racing a
        # finish (or a stale one from a since-abandoned session) is a
        # timing fact of life, not something the user did wrong.
        return JSONResponse({"ok": False}, status_code=404)
    return {"ok": True}


class PomodoroFinishIn(BaseModel):
    elapsed_seconds: int
    status: str  # "completed" | "stopped_early"
    break_duration_sec: int | None = None


@app.post("/pomodoro/sessions/{session_id}/finish")
def finish_pomodoro_session(session_id: int, payload: PomodoroFinishIn):
    row = queries.finish_pomodoro_session(
        session_id,
        elapsed_seconds=max(0, payload.elapsed_seconds),
        status=payload.status,
        break_duration_sec=payload.break_duration_sec,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


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


class ItemDueDateIn(BaseModel):
    due_date: str | None = None


@app.patch("/items/{item_id}/due-date")
def update_item_due_date(item_id: int, payload: ItemDueDateIn):
    # /calendar's drag-and-drop and "Today" shortcut both land here —
    # due_date alone, no lane/brief_date/status change (see
    # queries.set_item_due_date's own docstring for why that's a
    # different operation from move_item_to_date).
    if payload.due_date is not None:
        try:
            date.fromisoformat(payload.due_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid date: {payload.due_date!r}") from exc
    updated = queries.set_item_due_date(item_id, payload.due_date)
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


@app.post("/items/{item_id}/add-to-calendar")
def add_item_to_calendar(item_id: int):
    item = queries.get_item_for_calendar(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    result = calendar_sync.sync_item_to_calendar(item)
    return {"ok": True, **result}


class ItemReorderIn(BaseModel):
    after_item_id: int | None = None


@app.patch("/items/{item_id}/reorder")
def reorder_item(item_id: int, payload: ItemReorderIn):
    reordered = queries.reorder_item(item_id, payload.after_item_id)
    if not reordered:
        raise HTTPException(status_code=404, detail="Item not found, or after_item_id isn't a sibling")
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
    health = _health_snapshot()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "sources": queries.list_source_configs(),
            "assistant_last_indexed_at": index_status["last_indexed_at"],
            "assistant_chunk_count": index_status["chunk_count"],
            "health": health,
            "notify_configured": notify.is_configured(),
        },
    )


@app.post("/settings/notifications/test")
def send_test_notification():
    # Bypasses the once-a-day dedup deliberately — a test send needs an
    # immediate result every time it's clicked, not "already sent today."
    sent = notify.send("Daily Command Center", "Test notification from Settings.")
    if not sent:
        raise HTTPException(
            status_code=502,
            detail="Could not reach ntfy — check NTFY_TOPIC/NTFY_SERVER in .env." if notify.is_configured()
            else "NTFY_TOPIC isn't set in .env.",
        )
    return {"ok": True}


@app.get("/settings/activity")
def settings_activity(request: Request):
    # args_json/metadata_json are stored as raw JSON text — decoded here
    # so the template can read fields directly rather than needing a
    # custom Jinja filter just for this one page.
    tool_calls = []
    for row in queries.list_tool_calls():
        row = dict(row)
        row["args"] = json.loads(row["args_json"])
        tool_calls.append(row)

    task_events = []
    for row in queries.list_task_events():
        row = dict(row)
        row["metadata"] = json.loads(row["metadata_json"])
        task_events.append(row)

    return templates.TemplateResponse(
        request,
        "settings_activity.html",
        {"tool_calls": tool_calls, "task_events": task_events},
    )


@app.get("/settings/triage-rules")
def settings_triage_rules(request: Request):
    return templates.TemplateResponse(
        request,
        "settings_triage_rules.html",
        {
            "rules": queries.list_triage_rules(),
            "lanes": LANES,
            "lane_labels": queries.get_lane_labels(),
            "corrections": queries.list_triage_corrections(),
            "patterns": queries.triage_correction_patterns(),
        },
    )


class TriageRuleIn(BaseModel):
    field: str
    match_value: str
    lane: str


@app.post("/settings/triage-rules")
def create_triage_rule(payload: TriageRuleIn):
    if payload.field not in triage_rules.FIELDS:
        return JSONResponse(
            {"ok": False, "errors": {"field": f"Must be one of: {', '.join(triage_rules.FIELDS)}"}},
            status_code=400,
        )
    if not payload.match_value.strip():
        return JSONResponse(
            {"ok": False, "errors": {"match_value": "Enter text to match."}}, status_code=400
        )
    if payload.lane not in LANES:
        return JSONResponse(
            {"ok": False, "errors": {"lane": f"Unknown lane: {payload.lane!r}"}}, status_code=400
        )
    rule_id = queries.create_triage_rule(payload.field, payload.match_value.strip(), payload.lane)
    return JSONResponse({"ok": True, "id": rule_id})


class TriageRuleEnabledIn(BaseModel):
    enabled: bool


@app.patch("/settings/triage-rules/{rule_id}")
def update_triage_rule(rule_id: int, payload: TriageRuleEnabledIn):
    updated = queries.set_triage_rule_enabled(rule_id, payload.enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"ok": True}


@app.delete("/settings/triage-rules/{rule_id}")
def delete_triage_rule(rule_id: int):
    deleted = queries.delete_triage_rule(rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"ok": True}


@app.get("/settings/lane-labels")
def settings_lane_labels(request: Request):
    return templates.TemplateResponse(
        request,
        "settings_lane_labels.html",
        {"lanes": LANES, "lane_labels": queries.get_lane_labels()},
    )


class LaneLabelIn(BaseModel):
    label: str


@app.post("/settings/lane-labels/{lane}")
def update_lane_label(lane: str, payload: LaneLabelIn):
    if lane not in LANES:
        raise HTTPException(status_code=404, detail=f"Unknown lane: {lane!r}")
    queries.set_lane_label(lane, payload.label)
    return {"ok": True, "label": queries.get_lane_labels()[lane]}


def _rows_to_csv(rows: list[dict]) -> str:
    """Empty table -> empty string (no header row) rather than crashing
    on csv.DictWriter's fieldnames requirement — an empty CSV download
    is a legitimate, unsurprising result for "you have no data yet"."""
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


@app.get("/settings/export.json")
def export_json():
    # "Your data, not the repo's" — the same framing SETUP.md already
    # uses for the gitignored files. One file, everything in it, so
    # it's actually usable if you ever want to leave this instance
    # behind, not a paginated API for routine polling.
    payload = {
        "exported_at": datetime.now(TZ).isoformat(),
        "items": queries.export_items(),
        "finance_entries": queries.export_finance_entries(),
        "briefs": queries.export_briefs(),
    }
    return Response(
        content=json.dumps(payload, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=command-center-export.json"},
    )


@app.get("/settings/export/items.csv")
def export_items_csv():
    return Response(
        content=_rows_to_csv(queries.export_items()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=items.csv"},
    )


@app.get("/settings/export/finances.csv")
def export_finances_csv():
    return Response(
        content=_rows_to_csv(queries.export_finance_entries()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=finances.csv"},
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
