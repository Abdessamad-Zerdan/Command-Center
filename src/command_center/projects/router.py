"""GET/POST /projects (registry list + registration) and
GET/PATCH/DELETE /projects/{id} (detail page, sprint-field edits,
removal). Own APIRouter + own Jinja2Templates instance, same reasoning
as history/router.py and scheduling/router.py: app.py must import this
router, so this file can't import app.py's templates instance back
without a circular import.

Route collision check: /projects (list) is declared before
/projects/{id} (detail) in this same file, and no other router
registers a literal /projects/* path — so there's no risk of the
/history/report-vs-/history/{brief_date} style shadowing.
"""

import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from command_center import belongs, queries
from command_center.config import REPO_ROOT, TZ
from command_center.projects import fsutils
from command_center.pursuits import install_template_globals

LAUNCH_LOG_DIR = REPO_ROOT / "data" / "launch_logs"

router = APIRouter()
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
install_template_globals(templates)


def _relative_time(when: datetime) -> str:
    delta = datetime.now(TZ) - when
    seconds = delta.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = int(seconds // 86400)
    if days < 7:
        return f"{days} day{'' if days == 1 else 's'} ago"
    return when.strftime("%b %d, %Y")


def _last_worked_on(path: Path) -> dict[str, Any] | None:
    """Picks whichever signal is more recent — a git commit or a raw
    file mtime — since a project might be actively edited without a
    commit yet, or have commits from before the last uncommitted edit."""
    commit = fsutils.last_commit_info(path)
    modified = fsutils.last_modified_file(path)
    if commit and modified:
        if commit["timestamp"] >= modified["mtime"]:
            return {"detail": commit["message"], "when": commit["timestamp"]}
        return {"detail": modified["name"], "when": modified["mtime"]}
    if commit:
        return {"detail": commit["message"], "when": commit["timestamp"]}
    if modified:
        return {"detail": modified["name"], "when": modified["mtime"]}
    return None


class RegisterProjectIn(BaseModel):
    name: str
    path: str
    description: str = ""
    run_command: str = ""
    app_url: str = ""


_PROJECT_JSON_FIELDS = (
    "id", "name", "path", "description", "current_sprint", "sprint_goal", "blockers",
    "target_date", "run_command", "app_url", "created_at",
)


def _project_json(project: dict, stats: dict | None = None) -> dict:
    stats = stats or {}
    data = {key: project.get(key) or ("" if key not in ("id",) else project[key]) for key in _PROJECT_JSON_FIELDS}
    data["active"] = bool(project["active"])
    data["open_tasks"] = stats.get("open_tasks", 0)
    data["map_node_id"] = stats.get("map_node_id")
    data["page_url"] = belongs.project_url(project["id"])
    return data


@router.get("/projects")
def list_projects(request: Request):
    stats = queries.get_project_card_stats()
    projects = [_project_json(p, stats.get(p["id"])) for p in queries.list_registered_projects()]
    return templates.TemplateResponse(
        request,
        "projects.html",
        {"projects": projects, "today": datetime.now(TZ).date().isoformat(), "pursuits_tab": "projects"},
    )


@router.get("/projects/{project_id}/tasks")
def list_project_tasks(project_id: int):
    if queries.get_registered_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    items = sorted(
        queries.list_items_by_project(project_id),
        key=lambda i: (i["status"] != "pending", i["brief_date"]),
    )
    return [belongs.task_json(i) for i in items]


class ProjectTaskIn(BaseModel):
    title: str


@router.post("/projects/{project_id}/tasks")
def add_project_task(project_id: int, payload: ProjectTaskIn):
    """A task for this project on today's brief (Action items), already
    linked — the same as adding one from a competition or a Map card."""
    if queries.get_registered_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Give the task a name.")
    if len(title) > 300:
        raise HTTPException(status_code=400, detail="Task name is too long.")
    item_id = queries.create_manual_item(
        datetime.now(TZ).date().isoformat(), "action_items", title, project_id=project_id
    )
    return belongs.task_json(queries.get_item(item_id))


@router.post("/projects/{project_id}/to-map")
def send_project_to_map(project_id: int):
    """Pins the project on the first Map board — once; an existing card
    is reused."""
    if queries.get_registered_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    existing = queries.get_project_card_stats().get(project_id, {}).get("map_node_id")
    if existing:
        return {"ok": True, "node_id": existing, "existed": True}
    board = queries.get_or_create_default_board()
    node_id = queries.create_map_node(
        board["id"], "project", project_id=project_id,
        x=12 + (project_id * 19) % 55, y=12 + (project_id * 37) % 62,
    )
    return {"ok": True, "node_id": node_id, "existed": False}


@router.post("/projects")
def register_project(payload: RegisterProjectIn):
    name = payload.name.strip()
    path = payload.path.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name can't be empty")
    if not path:
        raise HTTPException(status_code=400, detail="Path can't be empty")
    if not Path(path).is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {path!r}")
    project_id = queries.create_registered_project(
        name, path, payload.description.strip(), payload.run_command.strip(), payload.app_url.strip()
    )
    return {"id": project_id}


@router.get("/projects/{project_id}")
def project_detail(request: Request, project_id: int):
    project = queries.get_registered_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # A project added from the assistant has no folder yet — Path("")
    # would quietly mean "the current directory", i.e. Command Center's
    # own repo, so skip the file-based signals entirely instead.
    has_folder = bool(project["path"]) and Path(project["path"]).is_dir()
    path = Path(project["path"])
    last_worked_on = _last_worked_on(path) if has_folder else None
    file_tree = fsutils.build_file_tree(path) if has_folder else None
    linked_items = queries.list_items_by_project(project_id)
    belongs.annotate(linked_items)

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "project": project,
            "linked_items": linked_items,
            "last_worked_on": last_worked_on,
            "relative_time": _relative_time(last_worked_on["when"]) if last_worked_on else None,
            "file_tree": file_tree,
            "file_count": fsutils.count_files(file_tree),
            "pursuits_tab": "projects",
        },
    )


class ProjectPatchIn(BaseModel):
    description: str | None = None
    current_sprint: str | None = None
    sprint_goal: str | None = None
    blockers: str | None = None
    target_date: str | None = None
    run_command: str | None = None
    app_url: str | None = None
    active: bool | None = None


@router.patch("/projects/{project_id}")
def patch_project(project_id: int, payload: ProjectPatchIn):
    updated = queries.update_registered_project(
        project_id,
        description=payload.description,
        current_sprint=payload.current_sprint,
        sprint_goal=payload.sprint_goal,
        blockers=payload.blockers,
        target_date=payload.target_date,
        run_command=payload.run_command,
        app_url=payload.app_url,
        active=payload.active,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"ok": True}


@router.delete("/projects/{project_id}")
def delete_project(project_id: int):
    deleted = queries.delete_registered_project(project_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"ok": True}


def _is_reachable(url: str, timeout: float = 1.0) -> bool:
    """A bare TCP connect, not a full HTTP request — cheaper, and avoids
    false negatives from a dev server that's up but returns a non-2xx
    status (e.g. a 404 on '/') during its own startup sequence."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_until_reachable(url: str, overall_timeout: float = 8.0, interval: float = 0.3) -> bool:
    deadline = time.monotonic() + overall_timeout
    while time.monotonic() < deadline:
        if _is_reachable(url, timeout=interval):
            return True
        time.sleep(interval)
    return False


def _clean_subprocess_env() -> dict[str, str]:
    """subprocess.Popen inherits Command Center's own environment by
    default. Since Command Center itself normally runs inside a venv (e.g.
    via `uv run`), that means VIRTUAL_ENV is set and its venv's Scripts/bin
    dir is prepended to PATH — so a launched project's bare 'python ...'
    run_command would silently resolve to *Command Center's* interpreter
    instead of the target project's own one. Reproduced: it found Command
    Center's uvicorn (both apps use FastAPI) but not the target project's
    own dependencies, and crashed with no visible error since the process
    was spawned detached. Stripping VIRTUAL_ENV and any PATH entry under it
    restores normal PATH-based interpreter resolution for the child."""
    env = os.environ.copy()
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        parts = env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(p for p in parts if not p.startswith(venv))
    # A detached child's stdout isn't a console, so a Python run_command
    # defaults to fully-buffered output — a crash's traceback can sit
    # unflushed in the process's own memory instead of reaching the log
    # file. Unbuffered makes the log actually useful when something fails.
    env["PYTHONUNBUFFERED"] = "1"
    return env


@router.post("/projects/{project_id}/launch")
def launch_project(project_id: int):
    """Starts a registered project's app (if it isn't already running) and
    hands back the URL to open. Detached so the process outlives this
    request — Windows needs its own flags for that, POSIX gets it from
    start_new_session. Blocks briefly (up to 8s) after a fresh spawn so the
    frontend can just window.open() the URL it gets back instead of guessing
    when the server will be ready."""
    project = queries.get_registered_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    app_url = project["app_url"]
    run_command = project["run_command"]

    if app_url and _is_reachable(app_url):
        return {"started": False, "url": app_url}

    if not run_command:
        raise HTTPException(status_code=400, detail="No run command configured for this project")

    # Captured to a file (not DEVNULL'd) so a launch that starts but never
    # comes up — wrong interpreter, a missing dependency, a port already
    # taken by something else — leaves a trail instead of failing silently.
    LAUNCH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LAUNCH_LOG_DIR / f"project_{project_id}.log"
    log_file = open(log_path, "ab")

    popen_kwargs: dict[str, Any] = {
        "cwd": project["path"],
        "shell": True,
        "env": _clean_subprocess_env(),
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(run_command, **popen_kwargs)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not start '{run_command}': {e}") from e
    finally:
        log_file.close()

    if app_url:
        _wait_until_reachable(app_url)
    return {"started": True, "url": app_url or None}
