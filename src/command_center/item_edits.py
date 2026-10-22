"""PATCH /items/{item_id}/... — editing one task's fields: title, lane,
due date, priority, its links (project, competition, research/target, map card)
and its place in the lane. Moved out of app.py (over the ~1,000-line
limit); every route still sits behind host_guard like all the others.
"""

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from command_center import queries
from command_center.config import LANES

router = APIRouter()


class ItemTitleIn(BaseModel):
    title: str


@router.patch("/items/{item_id}/title")
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


@router.patch("/items/{item_id}/lane")
def update_item_lane(item_id: int, payload: ItemLaneIn):
    if payload.lane not in LANES:
        raise HTTPException(status_code=400, detail=f"Unknown lane: {payload.lane!r}")
    updated = queries.update_item_lane(item_id, payload.lane)
    if not updated:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


class ItemPriorityIn(BaseModel):
    priority: int = Field(ge=1, le=3)


@router.patch("/items/{item_id}/priority")
def update_item_priority(item_id: int, payload: ItemPriorityIn):
    # The user's own High/Medium/Low (dropping a card in a priority
    # column) — locked, so the next prioritize.run leaves it alone.
    if not queries.set_item_priority(item_id, payload.priority):
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


class ItemDueDateIn(BaseModel):
    due_date: str | None = None


@router.patch("/items/{item_id}/due-date")
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


@router.patch("/items/{item_id}/project")
def update_item_project(item_id: int, payload: ItemProjectIn):
    updated = queries.update_item_project(item_id, payload.project_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


class ItemCompetitionIn(BaseModel):
    competition_id: int | None = None


@router.patch("/items/{item_id}/competition")
def update_item_competition(item_id: int, payload: ItemCompetitionIn):
    if payload.competition_id is not None and queries.get_competition(payload.competition_id) is None:
        raise HTTPException(status_code=400, detail="Unknown competition")
    if not queries.set_item_competition(item_id, payload.competition_id):
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


class ItemPursuitIn(BaseModel):
    pursuit_id: int | None = None


@router.patch("/items/{item_id}/pursuit")
def update_item_pursuit(item_id: int, payload: ItemPursuitIn):
    if payload.pursuit_id is not None and queries.get_pursuit(payload.pursuit_id) is None:
        raise HTTPException(status_code=400, detail="Unknown research topic or target")
    if not queries.set_item_pursuit(item_id, payload.pursuit_id):
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


class ItemMapNodeIn(BaseModel):
    map_node_id: int | None = None


@router.patch("/items/{item_id}/map-node")
def update_item_map_node(item_id: int, payload: ItemMapNodeIn):
    if payload.map_node_id is not None:
        node = queries.get_map_node(payload.map_node_id)
        if node is None or node["kind"] in ("label", "project"):
            raise HTTPException(status_code=400, detail="Unknown map card")
    if not queries.set_item_map_node(item_id, payload.map_node_id):
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


class ItemReorderIn(BaseModel):
    after_item_id: int | None = None


@router.patch("/items/{item_id}/reorder")
def reorder_item(item_id: int, payload: ItemReorderIn):
    reordered = queries.reorder_item(item_id, payload.after_item_id)
    if not reordered:
        raise HTTPException(status_code=404, detail="Item not found, or after_item_id isn't a sibling")
    return {"ok": True}
