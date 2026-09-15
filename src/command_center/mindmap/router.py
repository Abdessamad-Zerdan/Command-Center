"""GET /map — a freeform, hand-drawn-style board for the things that
don't fit a dated calendar: current projects (pinned by reference),
next month's targets, upcoming hackathons, competitions, and research.
Cards are positioned by drag, not by any grid or sort order — see
db.py's map_nodes table for why x/y are percentages.

Own APIRouter + own Jinja2Templates instance, same reasoning as every
other feature router in this app (calendar_view, projects, ...):
app.py imports this router, so importing app.py's own templates back
here would be circular.
"""

from datetime import date as date_type
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from command_center import queries
from command_center.config import TZ

router = APIRouter()
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _today() -> date_type:
    return datetime.now(TZ).date()


def _validate_date(value: str | None) -> None:
    if not value:
        return
    try:
        date_type.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date: {value!r}") from exc


@router.get("/map")
def mind_map_page(request: Request):
    return templates.TemplateResponse(
        request,
        "mindmap.html",
        {
            "nodes": queries.list_map_nodes(),
            "pinnable_projects": queries.list_pinnable_projects(),
            "kinds": queries.MAP_NODE_KINDS,
        },
    )


class MapNodeIn(BaseModel):
    kind: str
    title: str = ""
    note: str = ""
    target_date: str | None = None
    project_id: int | None = None
    x: float = 50.0
    y: float = 50.0


@router.post("/map/nodes")
def create_map_node_route(payload: MapNodeIn):
    if payload.kind not in queries.MAP_NODE_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind: {payload.kind!r}")
    _validate_date(payload.target_date)

    if payload.kind == "project":
        if payload.project_id is None:
            raise HTTPException(status_code=400, detail="project_id is required for kind='project'")
        if queries.get_registered_project(payload.project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        title, note, project_id = "", "", payload.project_id
    else:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title can't be empty")
        note, project_id = payload.note.strip(), None

    node_id = queries.create_map_node(
        kind=payload.kind,
        title=title,
        note=note,
        target_date=payload.target_date or None,
        project_id=project_id,
        x=max(0.0, min(100.0, payload.x)),
        y=max(0.0, min(100.0, payload.y)),
    )
    return {"ok": True, "id": node_id}


class MapNodePositionIn(BaseModel):
    x: float
    y: float


@router.patch("/map/nodes/{node_id}/position")
def update_map_node_position_route(node_id: int, payload: MapNodePositionIn):
    x = max(0.0, min(100.0, payload.x))
    y = max(0.0, min(100.0, payload.y))
    if not queries.update_map_node_position(node_id, x, y):
        raise HTTPException(status_code=404, detail="Node not found")
    return {"ok": True}


class MapNodeUpdateIn(BaseModel):
    title: str
    note: str = ""
    target_date: str | None = None


@router.patch("/map/nodes/{node_id}")
def update_map_node_route(node_id: int, payload: MapNodeUpdateIn):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title can't be empty")
    _validate_date(payload.target_date)

    if not queries.update_map_node(node_id, title=title, note=payload.note.strip(), target_date=payload.target_date or None):
        raise HTTPException(status_code=404, detail="Node not found, or it's a pinned project card (edit the project itself instead)")
    return {"ok": True}


@router.delete("/map/nodes/{node_id}")
def delete_map_node_route(node_id: int):
    if not queries.delete_map_node(node_id):
        raise HTTPException(status_code=404, detail="Node not found")
    return {"ok": True}
