"""/research and /targets: research topics and goals, laid out like the
Competitions page — a grid of cards, a popup grown out of the card, Full
details behind that. Both pages share this router and pursuits.html; the
kind ("research" or "target") is the only difference, down to their
statuses (queries.PURSUIT_STATUSES).

Every research/target card on the Map stands for one of these (see
db._adopt_map_pursuit_cards), so cards made on the Map show up here on
their own, and editing either side edits both.
"""

import re
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from command_center import attachments, belongs, queries
from command_center.config import TZ
from command_center.pursuits import install_template_globals

router = APIRouter()
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
install_template_globals(templates)

# Page copy per kind — everything else about the two pages is shared.
PAGE = {
    "research": {
        "tab": "research",
        "title": "Research",
        "heading": "Things you're digging into",
        "blurb": "Topics, questions and papers you're exploring. Each one keeps its notes, a date to aim for and the tasks that move it forward.",
        "add_label": "Add a topic",
        "empty_title": "No research yet",
        "empty_text": "Add a topic you want to dig into — or put a Research card on the Map and it shows up here.",
        "placeholder": "e.g. Hallucination in corporate RAG",
        "date_label": "Aim to finish by",
    },
    "target": {
        "tab": "targets",
        "title": "Targets",
        "heading": "Goals you're working towards",
        "blurb": "Bootcamps, certificates, launches — anything you want done by a date. Track it from planned to achieved.",
        "add_label": "Add a target",
        "empty_title": "No targets yet",
        "empty_text": "Add a goal with a date — or put a Target card on the Map and it shows up here.",
        "placeholder": "e.g. NVIDIA bootcamp certificate",
        "date_label": "Target date",
    },
}

_TEXT_LIMITS = {"title": 200, "notes": 4000, "url": 2000}


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _json(pursuit: dict, open_tasks: int | None = None, map_node_id: int | None = None) -> dict:
    stored = pursuit.get("cover_name")
    return {
        "id": pursuit["id"],
        "kind": pursuit["kind"],
        "title": pursuit["title"],
        "notes": pursuit.get("notes") or "",
        "status": pursuit["status"],
        "target_date": pursuit.get("target_date"),
        "url": pursuit.get("url") or "",
        # ?v= changes whenever the cover is replaced, so the long-lived
        # cache on the image URL never serves a stale one.
        "cover_url": f"/pursuits/{pursuit['id']}/cover?v={stored[:8]}" if stored else None,
        "open_tasks": pursuit.get("open_tasks", 0) if open_tasks is None else open_tasks,
        "map_node_id": pursuit.get("map_node_id") if map_node_id is None else map_node_id,
        "page_url": belongs.pursuit_url(pursuit),
        "created_at": pursuit["created_at"],
    }


def _fresh_json(pursuit_id: int) -> dict:
    pursuit = queries.get_pursuit(pursuit_id)
    open_tasks = sum(1 for i in queries.list_items_by_pursuit(pursuit_id) if i["status"] == "pending")
    node = queries.find_map_node_for_pursuit(pursuit_id)
    return _json(pursuit, open_tasks=open_tasks, map_node_id=node["id"] if node else 0)


def _get_or_404(pursuit_id: int) -> dict:
    pursuit = queries.get_pursuit(pursuit_id)
    if pursuit is None:
        raise HTTPException(status_code=404, detail="Not found")
    return pursuit


def _list_page(request: Request, kind: str):
    page = PAGE[kind]
    return templates.TemplateResponse(
        request,
        "pursuits.html",
        {
            "kind": kind,
            "page": page,
            "pursuits_tab": page["tab"],
            "items": [_json(p) for p in queries.list_pursuits(kind)],
            "statuses": [{"key": s, "label": belongs.status_label(s)} for s in queries.PURSUIT_STATUSES[kind]],
            "today": _today(),
        },
    )


def _detail_page(request: Request, kind: str, pursuit_id: int):
    pursuit = queries.get_pursuit(pursuit_id)
    if pursuit is None:
        return RedirectResponse(url=f"/{belongs.PURSUIT_PAGES[kind]}", status_code=302)
    if pursuit["kind"] != kind:
        return RedirectResponse(url=belongs.pursuit_url(pursuit), status_code=302)
    return templates.TemplateResponse(
        request, "focus.html", {"focus": belongs.focus_view(pursuit=pursuit), "today": _today()}
    )


@router.get("/research")
def research_page(request: Request):
    return _list_page(request, "research")


@router.get("/targets")
def targets_page(request: Request):
    return _list_page(request, "target")


@router.get("/research/{pursuit_id}")
def research_detail(request: Request, pursuit_id: int):
    return _detail_page(request, "research", pursuit_id)


@router.get("/targets/{pursuit_id}")
def target_detail(request: Request, pursuit_id: int):
    return _detail_page(request, "target", pursuit_id)


class PursuitIn(BaseModel):
    kind: str | None = None
    title: str | None = None
    notes: str | None = None
    status: str | None = None
    target_date: str | None = None
    url: str | None = None


def _clean(payload: PursuitIn, kind: str, *, creating: bool) -> dict:
    cleaned = {}
    for key, value in payload.model_dump(exclude_unset=True, exclude={"kind"}).items():
        if isinstance(value, str):
            value = value.strip()
        cleaned[key] = value or None

    if (creating or "title" in cleaned) and not cleaned.get("title"):
        raise HTTPException(status_code=400, detail="Give it a name.")
    for key, limit in _TEXT_LIMITS.items():
        if cleaned.get(key) and len(cleaned[key]) > limit:
            raise HTTPException(status_code=400, detail=f"{'Link' if key == 'url' else key.capitalize()} is too long.")

    # Same rule as competitions: the link becomes a clickable href, so only
    # web addresses get through.
    url = cleaned.get("url")
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.IGNORECASE):
            raise HTTPException(status_code=400, detail="The link must be a web address (http or https).")
        cleaned["url"] = "https://" + url

    if "status" in cleaned:
        if cleaned["status"] is None:
            del cleaned["status"]
        elif cleaned["status"] not in queries.PURSUIT_STATUSES[kind]:
            raise HTTPException(status_code=400, detail=f"Unknown status: {cleaned['status']!r}")

    if cleaned.get("target_date"):
        try:
            date.fromisoformat(cleaned["target_date"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="The date must be YYYY-MM-DD.") from exc
    return cleaned


@router.post("/pursuits")
def create_pursuit(payload: PursuitIn):
    if payload.kind not in queries.PURSUIT_KINDS:
        raise HTTPException(status_code=400, detail="Unknown kind")
    pursuit_id = queries.create_pursuit(payload.kind, _clean(payload, payload.kind, creating=True))
    return _fresh_json(pursuit_id)


@router.patch("/pursuits/{pursuit_id}")
def update_pursuit(pursuit_id: int, payload: PursuitIn):
    pursuit = _get_or_404(pursuit_id)
    queries.update_pursuit(pursuit_id, _clean(payload, pursuit["kind"], creating=False))
    return _fresh_json(pursuit_id)


@router.delete("/pursuits/{pursuit_id}")
def delete_pursuit(pursuit_id: int):
    pursuit = _get_or_404(pursuit_id)
    if pursuit["cover_name"]:
        attachments.delete_stored_file(pursuit["cover_name"])
    queries.delete_pursuit(pursuit_id)
    return {"ok": True}


@router.get("/pursuits/{pursuit_id}/tasks")
def list_tasks(pursuit_id: int):
    _get_or_404(pursuit_id)
    return [belongs.task_json(i) for i in queries.list_items_by_pursuit(pursuit_id)]


class PursuitTaskIn(BaseModel):
    title: str


@router.post("/pursuits/{pursuit_id}/tasks")
def add_task(pursuit_id: int, payload: PursuitTaskIn):
    """A task for this topic/target, added to today's brief already linked
    to it (Action items — the same neutral default as everywhere else)."""
    _get_or_404(pursuit_id)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Give the task a name.")
    if len(title) > 300:
        raise HTTPException(status_code=400, detail="Task name is too long.")
    item_id = queries.create_manual_item(_today(), "action_items", title)
    queries.set_item_pursuit(item_id, pursuit_id)
    return belongs.task_json(queries.get_item(item_id))


@router.post("/pursuits/{pursuit_id}/cover")
async def upload_cover(pursuit_id: int, file: UploadFile = File(...)):
    pursuit = _get_or_404(pursuit_id)
    content = await file.read(attachments.MAX_UPLOAD_BYTES + 1)
    try:
        stored_name, content_type, _size = attachments.save_upload(content)
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    queries.set_pursuit_cover(pursuit_id, stored_name, content_type)
    if pursuit["cover_name"]:
        attachments.delete_stored_file(pursuit["cover_name"])
    return _fresh_json(pursuit_id)


@router.delete("/pursuits/{pursuit_id}/cover")
def remove_cover(pursuit_id: int):
    pursuit = _get_or_404(pursuit_id)
    if pursuit["cover_name"]:
        attachments.delete_stored_file(pursuit["cover_name"])
    queries.set_pursuit_cover(pursuit_id, None, None)
    return _fresh_json(pursuit_id)


@router.get("/pursuits/{pursuit_id}/cover")
def get_cover(pursuit_id: int):
    pursuit = _get_or_404(pursuit_id)
    path = attachments.path_for(pursuit["cover_name"] or "")
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="No image")
    return FileResponse(
        path,
        media_type=pursuit["cover_type"],
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.post("/pursuits/{pursuit_id}/to-map")
def send_to_map(pursuit_id: int):
    """One card per topic/target: an existing one is reused."""
    pursuit = _get_or_404(pursuit_id)
    existing = queries.find_map_node_for_pursuit(pursuit_id)
    if existing is not None:
        return {"ok": True, "board_id": existing["board_id"], "node_id": existing["id"], "existed": True}
    board = queries.get_or_create_default_board()
    node_id = queries.create_map_node(
        board["id"], pursuit["kind"], title=pursuit["title"], note=pursuit["notes"] or "",
        target_date=pursuit["target_date"],
        x=12 + (pursuit_id * 23) % 55, y=12 + (pursuit_id * 31) % 62,
    )
    queries.link_map_node_to_pursuit(node_id, pursuit_id)
    return {"ok": True, "board_id": board["id"], "node_id": node_id, "existed": False}
