"""GET /map — a freeform, hand-drawn-style board for the things that
don't fit a dated calendar: current projects (pinned by reference),
next month's targets, upcoming hackathons, competitions, and research.
Cards are positioned by drag, not by any grid or sort order — see
db.py's map_nodes table for why x/y are percentages.

Multiple named boards (map_boards) let different stretches of time
(a "September" board, an "October" board, ...) live on separate
canvases instead of one crowded one. A kind='label' card (e.g. the
"September" heading itself) can have other cards on the same board
point at it via linked_label_id, drawn client-side as a hand-drawn
connector — see mindmap.html's connectorPaths.

Own APIRouter + own Jinja2Templates instance, same reasoning as every
other feature router in this app (calendar_view, projects, ...):
app.py imports this router, so importing app.py's own templates back
here would be circular.
"""

from datetime import date as date_type
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
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


def _resolve_linked_label_id(board_id: int, linked_label_id: int | None) -> int | None:
    """A link must point at a label that actually exists on this same
    board. Any card — including another label, making it a sublabel —
    may set this; nothing here stops a label linking to itself or a
    short cycle (A -> B -> A), a deliberate simplicity trade-off for a
    personal single-board tool rather than a public graph editor."""
    if linked_label_id is None:
        return None
    valid_ids = {label["id"] for label in queries.list_labels_on_board(board_id)}
    if linked_label_id not in valid_ids:
        raise HTTPException(status_code=400, detail="That label isn't on this board")
    return linked_label_id


@router.get("/map")
def mind_map_page(request: Request, board: int | None = None):
    current_board = queries.get_map_board(board) if board is not None else None
    if current_board is None:
        if board is not None:
            # Stale/bad link (e.g. a deleted board) — fall back instead of 404ing.
            return RedirectResponse(url="/map", status_code=302)
        current_board = queries.get_or_create_default_board()

    board_id = current_board["id"]
    return templates.TemplateResponse(
        request,
        "mindmap.html",
        {
            "boards": queries.list_map_boards(),
            "current_board": current_board,
            "nodes": queries.list_map_nodes(board_id),
            "pinnable_projects": queries.list_pinnable_projects(board_id),
            "kinds": queries.MAP_NODE_KINDS,
        },
    )


class MapBoardIn(BaseModel):
    name: str


@router.post("/map/boards")
def create_map_board_route(payload: MapBoardIn):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name can't be empty")
    return {"ok": True, "id": queries.create_map_board(name)}


@router.patch("/map/boards/{board_id}")
def rename_map_board_route(board_id: int, payload: MapBoardIn):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name can't be empty")
    if not queries.rename_map_board(board_id, name):
        raise HTTPException(status_code=404, detail="Board not found")
    return {"ok": True}


@router.delete("/map/boards/{board_id}")
def delete_map_board_route(board_id: int):
    if not queries.delete_map_board(board_id):
        raise HTTPException(status_code=404, detail="Board not found")
    return {"ok": True}


class MapNodeIn(BaseModel):
    board_id: int
    kind: str
    title: str = ""
    note: str = ""
    target_date: str | None = None
    project_id: int | None = None
    linked_label_id: int | None = None
    x: float = 50.0
    y: float = 50.0


@router.post("/map/nodes")
def create_map_node_route(payload: MapNodeIn):
    if queries.get_map_board(payload.board_id) is None:
        raise HTTPException(status_code=404, detail="Board not found")
    if payload.kind not in queries.MAP_NODE_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind: {payload.kind!r}")
    _validate_date(payload.target_date)
    linked_label_id = _resolve_linked_label_id(payload.board_id, payload.linked_label_id)

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
        board_id=payload.board_id,
        kind=payload.kind,
        title=title,
        note=note,
        target_date=payload.target_date or None,
        project_id=project_id,
        linked_label_id=linked_label_id,
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


class MapNodeLabelLinkIn(BaseModel):
    linked_label_id: int | None = None


@router.patch("/map/nodes/{node_id}/label")
def update_map_node_label_link_route(node_id: int, payload: MapNodeLabelLinkIn):
    # A separate endpoint from the one above rather than folding this
    # into MapNodeUpdateIn: update_map_node's WHERE project_id IS NULL
    # guard exists specifically to keep a pinned project's title/note
    # locked to the project it mirrors, but a project card should still
    # be linkable to a label (both the drag gesture and the picker work
    # on every kind) — that would be impossible if this shared the same
    # guarded write.
    existing = queries.get_map_node(node_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Node not found")
    linked_label_id = _resolve_linked_label_id(existing["board_id"], payload.linked_label_id)
    if not queries.update_map_node_label_link(node_id, linked_label_id):
        raise HTTPException(status_code=404, detail="Node not found")
    return {"ok": True}


@router.delete("/map/nodes/{node_id}")
def delete_map_node_route(node_id: int):
    if not queries.delete_map_node(node_id):
        raise HTTPException(status_code=404, detail="Node not found")
    return {"ok": True}
