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

from command_center import belongs, queries
from command_center.config import TZ
from command_center.pursuits import install_template_globals

router = APIRouter()
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
install_template_globals(templates)


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
def mind_map_page(request: Request, board: int | None = None, card: int | None = None):
    if card is not None and board is None:
        # A link to one card (e.g. from its own page) lands on its board.
        node = queries.get_map_node(card)
        if node is not None:
            board = node["board_id"]
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
            "pursuits_tab": "map",
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

    if payload.kind in queries.PURSUIT_KINDS:
        # A research/target card always stands for an entry on its page —
        # made here together, so the card shows up there right away.
        pursuit_id = queries.create_pursuit(
            payload.kind, {"title": title, "notes": note or None, "target_date": payload.target_date or None}
        )
    else:
        pursuit_id = None

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
    if pursuit_id is not None:
        queries.link_map_node_to_pursuit(node_id, pursuit_id)
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
    node = queries.get_map_node(node_id)
    if node and node["pursuit_id"]:
        # The card mirrors its Research/Targets entry — edit both.
        queries.update_pursuit(
            node["pursuit_id"],
            {"title": title, "notes": payload.note.strip() or None, "target_date": payload.target_date or None},
        )
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


def _get_node_or_404(node_id: int) -> dict:
    node = queries.get_map_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return node


@router.get("/map/nodes/{node_id}/detail")
def map_node_detail(node_id: int):
    """The breakdown a card click opens on the map — description, dates,
    what it stands for, and its tasks (see belongs.focus_view)."""
    node = _get_node_or_404(node_id)
    if node["kind"] == "label":
        raise HTTPException(status_code=400, detail="Labels don't have a breakdown")
    return belongs.focus_view(node=node)


class MapNodeTaskIn(BaseModel):
    title: str


@router.post("/map/nodes/{node_id}/tasks")
def add_map_node_task(node_id: int, payload: MapNodeTaskIn):
    """A task for this card, added to today's brief already linked to it
    (Action items — same neutral default as the competitions page and
    the assistant). A project or competition card links the task to the
    project/competition itself, see belongs.link_fields_for_node."""
    node = _get_node_or_404(node_id)
    if node["kind"] == "label":
        raise HTTPException(status_code=400, detail="Tasks go on a card, not a label")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Give the task a name.")
    if len(title) > 300:
        raise HTTPException(status_code=400, detail="Task name is too long.")
    item_id = queries.create_manual_item(_today().isoformat(), "action_items", title)
    belongs.apply_to_item(item_id, belongs.link_fields_for_node(node))
    return belongs.task_json(queries.get_item(item_id))


@router.get("/map/cards/{node_id}")
def map_card_page(request: Request, node_id: int):
    """A card's own page — the place every "belongs to" chip and mention
    of the card links to. A project card's page is the project's page."""
    node = queries.get_map_node(node_id)
    if node is None or node["kind"] == "label":
        return RedirectResponse(url="/map", status_code=302)
    # A card that stands for something else has that thing's page instead.
    if node["project_id"]:
        return RedirectResponse(url=belongs.project_url(node["project_id"]), status_code=302)
    if node["competition_id"]:
        return RedirectResponse(url=belongs.competition_url(node["competition_id"]), status_code=302)
    if node["pursuit_id"]:
        pursuit = queries.get_pursuit(node["pursuit_id"])
        if pursuit is not None:
            return RedirectResponse(url=belongs.pursuit_url(pursuit), status_code=302)
    return templates.TemplateResponse(
        request, "focus.html", {"focus": belongs.focus_view(node=node), "today": _today().isoformat()}
    )
