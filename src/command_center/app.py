import csv
import io
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from command_center import attachments, auth, belongs, timestamps, calendar_sync, fixtures, license_gate, notify, nudges, pipeline, queries, setup_wizard, triage_rules
from command_center import build_css, carryover, host_guard, reading, refresh
from command_center.assistant import ingest as assistant_ingest
from command_center.assistant.router import router as assistant_router
from command_center.calendar_view import router as calendar_view_router
from command_center.calendar_view.router import router as calendar_router
from command_center.competitions.router import router as competitions_router
from command_center.finances.router import router as finances_router
from command_center.history.router import router as history_router
from command_center.mindmap.router import router as mindmap_router
from command_center.projects.router import router as projects_router
from command_center.pursuits.router import router as pursuits_router
from command_center.scheduling.router import router as scheduling_router
from command_center.item_edits import router as item_edits_router
from command_center.sources.router import router as sources_router
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
timestamps.install(templates)
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
        # Deliberate: one source's failed pull must not stop the others
        # due this tick, or kill the scheduler loop.
        except Exception:
            logger.exception("Scheduled pull failed for source %s", cfg["source_name"])
    try:
        _notify_new_nudges()
    # Deliberate: desktop nudges are a nice-to-have; a failure here must
    # never take down the scheduler loop.
    except Exception:
        logger.exception("Nudge notification check failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _scheduler
    license_gate.require()  # see license_gate.py — refuses to start without a valid key
    init_db()
    pipeline.seed_source_config()  # last_pulled_at seeded to now — no pull-on-startup
    queries.seed_app_settings()  # day-bounds defaults, never clobbers existing values
    triage_rules.seed_builtin_rules()  # once per install — see triage_rules.BUILTIN_RULES
    # /calendar then opens on cached Google events instead of waiting ~2.5 s.
    calendar_view_router.warm_current_month()
    stale_css = build_css.newer_than_css()
    if stale_css:
        # A template edited without `uv run make-css` can use a class the
        # prebuilt CSS doesn't have — say so where the developer looks.
        logger.warning(
            "static/css/app.css may be out of date (%d newer file(s), e.g. %s) — run `uv run make-css`.",
            len(stale_css), stale_css[0].name,
        )
    try:
        assistant_ingest.rebuild_index()  # no-op if data/vision.md is unchanged
    except Exception:
        # First-run model download can fail offline; degrade rather than
        # block startup — same "never fail the whole brief" philosophy
        # as every other source in pipeline.py.
        logger.exception("Assistant index build failed at startup")
    if auth.has_valid_credentials():
        if queries.get_brief(_today()) is None:
            # In the background: the app answers right away and /brief
            # shows the latest brief with a "Refreshing…" banner meanwhile
            # (see refresh.py) instead of nothing for about a minute.
            refresh.start()
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
host_guard.install(app)  # refuses foreign Host / cross-site Origin — see host_guard.py (DNS rebinding)
class _RevalidatedStatic(StaticFiles):
    """/static with "Cache-Control: no-cache": the browser keeps its copy
    and revalidates it (ETag → 304, a few ms on localhost) instead of
    downloading app.css and the scripts on every page — and a rebuilt
    file is picked up at once, with no version stamps in the URLs."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _RevalidatedStatic(directory=str(STATIC_DIR)), name="static")
setup_wizard.register(app)  # remove this line (+ the setup_wizard package) to remove the wizard
app.include_router(assistant_router)
app.include_router(calendar_router)
app.include_router(finances_router)
app.include_router(history_router)  # before any @app.get("/history/{brief_date}") is registered below
app.include_router(scheduling_router)
app.include_router(item_edits_router)
app.include_router(sources_router)
app.include_router(projects_router)
app.include_router(mindmap_router)
app.include_router(competitions_router)
app.include_router(pursuits_router)

# Timeline strip window — business hours are what fit on a phone without scrolling.
TIMELINE_START_HOUR = 8
TIMELINE_END_HOUR = 20


def _timeline_pct(dt: datetime) -> float:
    window_minutes = (TIMELINE_END_HOUR - TIMELINE_START_HOUR) * 60
    minutes_in = (dt.hour - TIMELINE_START_HOUR) * 60 + dt.minute
    return max(0.0, min(100.0, (minutes_in / window_minutes) * 100))


def _to_app_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _position_events(events: list[dict]) -> list[dict]:
    positioned = []
    for ev in events:
        start = _to_app_tz(datetime.fromisoformat(ev["start_time"]))
        end = _to_app_tz(datetime.fromisoformat(ev["end_time"]))
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


def _annotate_links(lanes: dict[str, list[dict]]) -> None:
    """Mutates each item in-place with its "belongs to" chips (project,
    competition, map card) — the clickable chips on a task card."""
    belongs.annotate([item for items in lanes.values() for item in items])


def _cards_for_linking() -> list[dict]:
    """Options for the task popup's "Research / target" picker: Research
    and Targets entries ("p:ID"), then any other Map card a task can
    belong to ("n:ID" — e.g. a hackathon not saved on Competitions). A
    card standing for a competition or an entry is left out: picking that
    thing itself covers it."""
    options = [
        {"key": f"p:{p['id']}", "id": p["id"], "title": p["title"], "kind": p["kind"],
         "where": belongs.PURSUIT_PAGE_TITLES[p["kind"]]}
        for kind in queries.PURSUIT_KINDS
        for p in queries.list_pursuits(kind)
    ]
    options += [
        {"key": f"n:{n['id']}", "id": n["id"], "title": n["title"], "kind": n["kind"], "where": n["board_name"]}
        for n in queries.list_linkable_map_cards()
        if not n["competition_id"] and not n["pursuit_id"]
    ]
    return options


def _competitions_for_linking() -> list[dict]:
    """Options for the task popup's Competition picker — everything not
    passed on (a task already linked to a passed one still shows it,
    via the item's own competition_title)."""
    return [
        {"id": c["id"], "title": c["title"], "kind": c["kind"]}
        for c in queries.list_competitions()
        if c["status"] != "passed"
    ]


def _annotate_attachment_counts(lanes: dict[str, list[dict]]) -> None:
    """Mutates each item in-place with attachment_count — drives the
    small photo badge on a task card."""
    all_items = [item for items in lanes.values() for item in items]
    counts = queries.get_attachment_counts([item["id"] for item in all_items])
    for item in all_items:
        item["attachment_count"] = counts.get(item["id"], 0)


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


def _brief_view(request: Request) -> str:
    """The brief's layout (partials/lanes.html): "priority" (High/Medium/
    Low columns, the default) or "sections" (one column per lane). A
    cookie, so the choice holds across reloads — only these two values
    are ever used, whatever the cookie says."""
    return "sections" if request.cookies.get("brief_view") == "sections" else "priority"


def _prioritize_failed_at() -> str | None:
    """"HH:MM" of the last failed priority sort, if the latest one failed
    — so the board says its priorities are stale instead of hiding it."""
    value = queries.get_app_value("prioritize_failed_at")
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(TZ).strftime("%H:%M")


@app.get("/brief")
def dashboard(request: Request):
    brief_date = _today()
    queries.wake_snoozed_items(brief_date)
    brief = queries.get_brief(brief_date)
    refresh_status = refresh.status()
    showing_previous = None
    if brief is None:
        # Today's first pull hasn't landed yet — usually because it's still
        # running in the background (see refresh.py). Show the latest brief
        # meanwhile, with a banner, rather than an empty page.
        # fixtures.seed()'s demo mode guarantees a row whenever Google isn't
        # connected, so this is the "connected, first pull not in yet" case.
        previous = queries.latest_brief_date_before(brief_date)
        brief = queries.get_brief(previous) if previous else None
        if brief is None:
            return templates.TemplateResponse(request, "brief_not_ready.html", {"refresh": refresh_status})
        showing_previous = previous

    carryover.add_carried_over(brief, brief_date)
    reading.cap_lane(brief["lanes"], brief_date)
    _annotate_time_logged(brief["lanes"])
    _annotate_attachment_counts(brief["lanes"])
    _annotate_links(brief["lanes"])
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
            "link_competitions": _competitions_for_linking(),
            "link_cards": _cards_for_linking(),
            "nudges": nudges.compute_nudges(brief_date),
            # The real today, even while yesterday's brief is shown — the
            # overdue nudge is computed against it, so "Bring to today"
            # must move tasks to it too, or they stay overdue.
            "today": brief_date,
            "brief_view": _brief_view(request),
            "prioritize_failed_at": _prioritize_failed_at(),
            "refresh": refresh_status,
            "showing_previous": showing_previous,
        },
    )


@app.get("/brief/refresh-status")
def brief_refresh_status():
    """Polled by the "Refreshing…" banner; it reloads the page once
    `running` turns false."""
    return {**refresh.status(), "today_ready": queries.get_brief(_today()) is not None}


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
    _annotate_attachment_counts(brief["lanes"])
    _annotate_links(brief["lanes"])
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
            "link_competitions": _competitions_for_linking(),
            "link_cards": _cards_for_linking(),
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
    if brief_date == _today():
        queries.wake_snoozed_items(brief_date)
    brief = queries.get_brief(brief_date)
    if brief is None:
        raise HTTPException(status_code=404, detail="No brief for that date")

    carryover.add_carried_over(brief, _today())
    reading.cap_lane(brief["lanes"], _today())
    _annotate_time_logged(brief["lanes"])
    _annotate_attachment_counts(brief["lanes"])
    _annotate_links(brief["lanes"])
    html = templates.env.get_template("partials/lanes.html").render(
        request=request,
        brief=brief,
        lane_labels=queries.get_lane_labels(),
        lanes_order=LANES,
        is_history=brief_date != _today(),
        brief_view=_brief_view(request),
        prioritize_failed_at=_prioritize_failed_at(),
    )
    return JSONResponse({"html": html, "lane_counts": _lane_counts(brief["lanes"])})


@app.post("/items/{item_id}/done")
def mark_done(item_id: int):
    queries.set_item_status(item_id, "done")
    return {"ok": True}


@app.post("/items/{item_id}/snooze")
def mark_snoozed(item_id: int):
    # "Later": off today's brief, back on tomorrow's (wake_snoozed_items).
    tomorrow = (date.fromisoformat(_today()) + timedelta(days=1)).isoformat()
    queries.set_item_status(item_id, "snoozed", snoozed_until=tomorrow)
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
        # Background, like startup — the brief comes back immediately with
        # a "Refreshing…" banner and reloads itself when the run is done.
        refresh.start()
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
    # Set when it's added from a High/Medium/Low column — the user chose
    # it, so it's locked like any priority they set.
    priority: int | None = Field(default=None, ge=1, le=3)


@app.post("/items")
def create_item(payload: ManualItemIn):
    if payload.lane not in LANES:
        raise HTTPException(status_code=400, detail=f"Unknown lane: {payload.lane!r}")
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="Title can't be empty")
    due_date = payload.due_date or None
    if due_date is not None:
        try:
            date.fromisoformat(due_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid date: {due_date!r}") from exc
    item_id = queries.create_manual_item(_today(), payload.lane, payload.title.strip(), due_date=due_date)
    if payload.priority is not None:
        queries.set_item_priority(item_id, payload.priority)
    return {"id": item_id}


@app.post("/items/{item_id}/add-to-calendar")
def add_item_to_calendar(item_id: int):
    item = queries.get_item_for_calendar(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    result = calendar_sync.sync_item_to_calendar(item)
    return {"ok": True, **result}


def _item_json(item: dict) -> dict:
    """An item as the task popup expects it, link names included."""
    item = dict(item)
    belongs.annotate([item])
    return {
        **item,
        "project_name": item.get("project_name"),
        "competition_title": item.get("competition_title"),
        "map_node_title": item.get("map_node_title"),
        "pursuit_title": item.get("pursuit_title"),
        "attachment_count": queries.get_attachment_counts([item["id"]]).get(item["id"], 0),
    }


@app.get("/items/{item_id}")
def get_item(item_id: int):
    item = queries.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return _item_json(item)


@app.get("/items/{item_id}/open")
def open_item(item_id: int):
    """A task's permanent address: sends you to the day it lives on with
    the task opened there. Anything that refers to a task elsewhere in
    the app (a Pomodoro entry, a project, a competition) links here."""
    item = queries.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="That task no longer exists")
    page = "/brief" if item["brief_date"] == _today() else f"/history/{item['brief_date']}"
    return RedirectResponse(url=f"{page}?open={item_id}", status_code=303)


def _attachment_json(attachment: dict) -> dict:
    return {
        "id": attachment["id"],
        "url": f"/attachments/{attachment['id']}",
        "original_name": attachment["original_name"],
        "size_bytes": attachment["size_bytes"],
        "created_at": attachment["created_at"],
    }


@app.get("/items/{item_id}/attachments")
def list_item_attachments(item_id: int):
    return [_attachment_json(a) for a in queries.list_item_attachments(item_id)]


@app.post("/items/{item_id}/attachments")
async def upload_item_attachment(item_id: int, file: UploadFile = File(...)):
    if not queries.item_exists(item_id):
        raise HTTPException(status_code=404, detail="Item not found")
    # Read at most one byte past the limit — enough to reject an oversized
    # upload without ever holding the whole thing in memory.
    content = await file.read(attachments.MAX_UPLOAD_BYTES + 1)
    try:
        stored_name, content_type, size_bytes = attachments.save_upload(content)
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    original_name = (file.filename or "photo")[:200]
    attachment_id = queries.create_item_attachment(item_id, stored_name, original_name, content_type, size_bytes)
    return _attachment_json(queries.get_item_attachment(attachment_id))


@app.get("/attachments/{attachment_id}")
def get_attachment_file(attachment_id: int):
    attachment = queries.get_item_attachment(attachment_id)
    path = attachments.path_for(attachment["stored_name"]) if attachment else None
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Photo not found")
    return FileResponse(
        path,
        media_type=attachment["content_type"],
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.delete("/attachments/{attachment_id}")
def delete_attachment(attachment_id: int):
    attachment = queries.get_item_attachment(attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    attachments.delete_stored_file(attachment["stored_name"])
    queries.delete_item_attachment(attachment_id)
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


@app.delete("/pomodoro/tasks")
def delete_pomodoro_task(name: str):
    deleted = queries.delete_pomodoro_task(name)
    if not deleted:
        raise HTTPException(status_code=404, detail="No sessions under that name")
    return {"deleted": deleted}


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
            "summary_language": queries.get_summary_language(),
        },
    )


class SummaryLanguageIn(BaseModel):
    language: str


@app.post("/settings/summary-language")
def set_summary_language(payload: SummaryLanguageIn):
    language = " ".join(payload.language.split())
    # A language name, not a sentence — it's dropped straight into the triage prompt.
    if not language or len(language) > 40 or not all(ch.isalpha() or ch in " -()" for ch in language):
        raise HTTPException(status_code=400, detail="Enter a language name, e.g. English or Türkçe.")
    queries.set_summary_language(language)
    return {"ok": True, "language": language}


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
            "categories": triage_rules.GMAIL_CATEGORIES,
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
    if payload.field == "bulk":
        payload.match_value = "yes"  # nothing to match on — the field is the condition
    if not payload.match_value.strip():
        return JSONResponse(
            {"ok": False, "errors": {"match_value": "Enter text to match."}}, status_code=400
        )
    if payload.field == "category" and payload.match_value.strip().lower() not in triage_rules.GMAIL_CATEGORIES:
        return JSONResponse(
            {"ok": False, "errors": {"match_value": f"Category must be one of: {', '.join(triage_rules.GMAIL_CATEGORIES)}"}},
            status_code=400,
        )
    if payload.lane not in triage_rules.RULE_LANES:
        return JSONResponse(
            {"ok": False, "errors": {"lane": f"Unknown lane: {payload.lane!r}"}}, status_code=400
        )
    rule_id = queries.create_triage_rule(payload.field, payload.match_value.strip().lower() if payload.field == "category" else payload.match_value.strip(), payload.lane)
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
