"""Wizard routes — GET renders a step (pre-filled from state if
revisiting it), POST validates + saves + advances. Every POST returns
JSON (`{"ok": bool, "next_url": ..., "errors": {...}}`) so the client can
show inline errors without losing already-entered data on a full reload.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from command_center import auth, queries, triage
from command_center.config import TZ
from command_center.setup_wizard import finalize, invites, live_config, state as state_module, validators
from command_center.sources import RawItem, medium
from command_center.sources.calendar import CalendarSource
from command_center.sources.gmail import GmailSource
from command_center.sources.tasks import TasksSource

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/setup")

# Not under the /setup prefix — /settings/invites is the admin page that
# *creates* invites, reachable only from an already-set-up instance (the
# gating middleware in register.py redirects everything outside /setup
# to /setup while incomplete, so this is admin-only for free).
settings_router = APIRouter()

STEP_TEMPLATES = {
    1: "setup/welcome.html",
    2: "setup/profile.html",
    7: "setup/groq.html",
    8: "setup/medium.html",
    9: "setup/google.html",
    10: "setup/test.html",
}

REQUIRED_SERVICES = {"groq"}  # Part 2 extends this to add gmail/calendar


def _step_url(step: int) -> str:
    return f"/setup/step/{step}"


def _step_context(step: int, state: dict) -> dict:
    return {
        "current_step": step,
        "total_steps": state_module.TOTAL_STEPS,
        "prev_url": _step_url(state_module.prev_step(step)) if state_module.prev_step(step) else None,
        "state": state,
        "required_services": sorted(REQUIRED_SERVICES),
    }


@router.get("")
def setup_root():
    state = state_module.load()
    return RedirectResponse(url=_step_url(state["current_step"]), status_code=303)


@router.get("/step/{step}")
def get_step(step: int, request: Request):
    state = state_module.load()
    template = STEP_TEMPLATES.get(step)
    if template is None:
        return RedirectResponse(url=_step_url(state["current_step"]), status_code=303)
    return templates.TemplateResponse(request, template, _step_context(step, state))


@router.post("/back")
def go_back():
    state = state_module.load()
    prev = state_module.prev_step(state["current_step"])
    if prev is not None:
        state["current_step"] = prev
        state_module.save(state)
    return RedirectResponse(url=_step_url(state["current_step"]), status_code=303)


def _advance(state: dict, from_step: int) -> str | None:
    nxt = state_module.next_step(from_step)
    state["current_step"] = nxt if nxt is not None else from_step
    state_module.save(state)
    return _step_url(nxt) if nxt is not None else None


@router.post("/step/1")
def post_welcome():
    state = state_module.load()
    next_url = _advance(state, 1)
    return JSONResponse({"ok": True, "next_url": next_url})


class ProjectIn(BaseModel):
    name: str
    sentence: str
    status_tag: str = "Shipping"
    link: str | None = None


class ProfileIn(BaseModel):
    name: str
    title: str
    tagline: str
    projects: list[ProjectIn] = []


@router.post("/step/2")
def post_profile(payload: ProfileIn):
    errors: dict[str, str] = {}
    if not payload.name.strip():
        errors["name"] = "Enter your name."
    if not payload.title.strip():
        errors["title"] = "Enter a short title."
    if not payload.tagline.strip():
        errors["tagline"] = "Enter a one-line bio."
    for i, project in enumerate(payload.projects):
        if not project.name.strip() or not project.sentence.strip():
            errors[f"project_{i}"] = "Each project needs a name and a one-line description."
            break

    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    state = state_module.load()
    state["profile"] = {
        "name": payload.name.strip(),
        "title": payload.title.strip(),
        "tagline": payload.tagline.strip(),
        "projects": [p.model_dump() for p in payload.projects],
    }
    next_url = _advance(state, 2)
    return JSONResponse({"ok": True, "next_url": next_url})


class GroqKeyIn(BaseModel):
    groq_api_key: str


@router.post("/step/7")
def post_groq(payload: GroqKeyIn):
    error = validators.validate_groq_key(payload.groq_api_key)
    if error:
        return JSONResponse({"ok": False, "errors": {"groq_api_key": error}}, status_code=400)

    state = state_module.load()
    state["credentials"]["groq_api_key"] = payload.groq_api_key.strip()
    next_url = _advance(state, 7)
    return JSONResponse({"ok": True, "next_url": next_url})


class MediumIn(BaseModel):
    medium_api_key: str = ""
    medium_username: str = ""
    skip: bool = False


@router.post("/step/8")
def post_medium(payload: MediumIn):
    state = state_module.load()

    if payload.skip:
        state["credentials"]["medium_api_key"] = ""
        state["credentials"]["medium_username"] = ""
        state["confirmations"]["medium_skipped"] = True
    else:
        key = payload.medium_api_key.strip()
        username = payload.medium_username.strip()
        if not key or not username:
            return JSONResponse(
                {
                    "ok": False,
                    "errors": {"medium": "Enter both fields, or use Skip for now."},
                },
                status_code=400,
            )
        state["credentials"]["medium_api_key"] = key
        state["credentials"]["medium_username"] = username
        state["confirmations"]["medium_skipped"] = False

    next_url = _advance(state, 8)
    return JSONResponse({"ok": True, "next_url": next_url})


@router.post("/step/9")
def post_google_step():
    # Real Google OAuth in the wizard is the still-unbuilt "Part 2" (see
    # STEP_ORDER in state.py) — this step just acknowledges and moves on,
    # same as a no-op-and-advance always did. See templates/setup/google.html
    # for what the user actually sees here.
    state = state_module.load()
    next_url = _advance(state, 9)
    return JSONResponse({"ok": True, "next_url": next_url})


def _dummy_raw_item() -> RawItem:
    return RawItem(
        source="setup_wizard",
        source_id="connectivity-check",
        title="Connectivity check",
        body="Used to validate the Groq API key during setup.",
        metadata={},
    )


def _run_diagnostics(state: dict) -> dict:
    results: dict[str, dict] = {}

    groq_key = state["credentials"]["groq_api_key"]
    try:
        with (
            live_config.temporary_patch("GROQ_API_KEY", groq_key),
            # The fresh-install default TRIAGE_MODEL (llama3.2:3b) is an
            # Ollama tag, not a Groq one — without this, the live test
            # would 404 with model_not_found on every brand-new instance.
            live_config.temporary_patch("TRIAGE_MODEL", finalize.DEFAULT_GROQ_MODEL),
        ):
            triage._run_groq([_dummy_raw_item()])
        results["groq"] = {"pass": True, "message": "Connected."}
    except Exception as exc:
        results["groq"] = {"pass": False, "message": str(exc)}

    try:
        credentials = auth.get_google_credentials()
    except Exception as exc:
        message = str(exc)
        results["gmail"] = {"pass": False, "message": message}
        results["calendar"] = {"pass": False, "message": message}
        results["tasks"] = {"pass": False, "message": message}
    else:
        checks = (
            ("gmail", lambda: GmailSource(credentials).fetch()),
            ("calendar", lambda: CalendarSource(credentials).fetch_with_events()),
            ("tasks", lambda: TasksSource(credentials).fetch()),
        )
        for name, run in checks:
            try:
                run()
                results[name] = {"pass": True, "message": "Connected."}
            except Exception as exc:
                results[name] = {"pass": False, "message": str(exc)}

    if state["confirmations"]["medium_skipped"] or not state["credentials"]["medium_api_key"]:
        results["medium"] = {"pass": None, "message": "Skipped."}
    else:
        try:
            with (
                live_config.temporary_patch("MEDIUM_API_KEY", state["credentials"]["medium_api_key"]),
                live_config.temporary_patch("MEDIUM_USERNAME", state["credentials"]["medium_username"]),
            ):
                medium.fetch_and_rank()
            results["medium"] = {"pass": True, "message": "Connected."}
        except Exception as exc:
            results["medium"] = {"pass": False, "message": str(exc)}

    return results


@router.post("/step/10")
def post_run_tests():
    state = state_module.load()
    results = _run_diagnostics(state)
    state["test_results"] = results
    state_module.save(state)
    ready = all((results.get(name) or {}).get("pass") for name in REQUIRED_SERVICES)
    return JSONResponse({"ok": True, "results": results, "ready_to_finish": ready})


@router.post("/finish")
def post_finish(request: Request):
    state = state_module.load()
    results = state.get("test_results", {})
    if not all((results.get(name) or {}).get("pass") for name in REQUIRED_SERVICES):
        return JSONResponse(
            {"ok": False, "errors": {"finish": "Run tests first — required services haven't passed yet."}},
            status_code=400,
        )
    try:
        finalize.finish(state)
    except OSError as exc:
        # Disk-full, permission-denied, etc. writing profile.py/.env — a
        # one-time setup action, but still shouldn't surface a raw 500
        # when the wizard's own JSON-error convention already has a
        # place to show this (test.html's finishError).
        logger.exception("Setup finish failed to write config")
        return JSONResponse(
            {"ok": False, "errors": {"finish": f"Could not write config: {exc}."}},
            status_code=500,
        )
    # One invite, one use — the gate already required a valid token to
    # reach this point, so it's always present here in practice; the
    # guard is defensive, not a real branch this app's own flow takes.
    if token := state.get("invite_token"):
        queries.mark_setup_invite_used(token, request.client.host if request.client else None)
    return JSONResponse({"ok": True, "next_url": "/"})


def _invite_view(row: dict) -> dict:
    return {**row, "status": invites.invite_status(row), "link": f"/setup?invite={row['token']}"}


@settings_router.get("/settings/invites")
def get_invites_page(request: Request):
    invite_rows = [_invite_view(row) for row in queries.list_setup_invites()]
    return templates.TemplateResponse(request, "settings_invites.html", {"invites": invite_rows})


class InviteIn(BaseModel):
    email: str
    expiry_days: int = invites.DEFAULT_EXPIRY_DAYS


@settings_router.post("/settings/invites")
def create_invite(payload: InviteIn):
    email = payload.email.strip()
    if not email:
        return JSONResponse({"ok": False, "errors": {"email": "Enter an email address."}}, status_code=400)
    if payload.expiry_days < 1:
        return JSONResponse(
            {"ok": False, "errors": {"expiry_days": "Must be at least 1 day."}}, status_code=400
        )

    token = invites.generate_token()
    expires_at = (datetime.now(TZ) + timedelta(days=payload.expiry_days)).isoformat()
    queries.create_setup_invite(email, token, expires_at)
    row = queries.get_setup_invite_by_token(token)
    return JSONResponse({"ok": True, "invite": _invite_view(row)})
