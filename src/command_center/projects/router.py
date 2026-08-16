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

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from command_center import queries
from command_center.config import TZ
from command_center.projects import fsutils

router = APIRouter()
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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


@router.get("/projects")
def list_projects(request: Request):
    projects = queries.list_registered_projects()
    return templates.TemplateResponse(request, "projects.html", {"projects": projects})


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
    project_id = queries.create_registered_project(name, path, payload.description.strip())
    return {"id": project_id}


@router.get("/projects/{project_id}")
def project_detail(request: Request, project_id: int):
    project = queries.get_registered_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    path = Path(project["path"])
    last_worked_on = _last_worked_on(path)
    file_tree = fsutils.build_file_tree(path)

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "project": project,
            "linked_items": queries.list_items_by_project(project_id),
            "last_worked_on": last_worked_on,
            "relative_time": _relative_time(last_worked_on["when"]) if last_worked_on else None,
            "file_tree": file_tree,
            "file_count": fsutils.count_files(file_tree),
        },
    )


class ProjectPatchIn(BaseModel):
    description: str | None = None
    current_sprint: str | None = None
    sprint_goal: str | None = None
    blockers: str | None = None
    target_date: str | None = None
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
