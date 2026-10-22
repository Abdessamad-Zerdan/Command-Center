"""/competitions — a shortlist of hackathons and competitions spotted
online, kept to decide on later. Each entry is a screenshot plus a few
details (deadline, prize, where, notes) and a status (saved →
shortlisted → applied, or passed). Screenshots reuse attachments.py's
storage and validation. Anything worth committing to can be sent on to
the Map as a hackathon/competition card.
"""

import re
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from command_center import attachments, belongs, queries, timestamps
from command_center.config import TZ
from command_center.pursuits import install_template_globals

router = APIRouter()

# A fresh Jinja2Templates instance, same reasoning as finances/router.py.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
install_template_globals(templates)

KINDS = ("hackathon", "competition", "fellowship", "other")
STATUSES = ("saved", "shortlisted", "applied", "passed")

# Map card kinds: the Map has hackathon/competition kinds of its own;
# anything else lands as a plain target.
_MAP_KIND = {"hackathon": "hackathon", "competition": "competition", "fellowship": "target", "other": "target"}

_TEXT_LIMITS = {
    "title": 200, "url": 2000, "organizer": 120, "event_dates": 80,
    "location": 80, "prize": 80, "notes": 4000,
}
_LABELS = {"event_dates": "Event dates", "url": "Link"}


class CompetitionIn(BaseModel):
    title: str | None = None
    url: str | None = None
    kind: str | None = None
    organizer: str | None = None
    deadline: str | None = None
    event_dates: str | None = None
    location: str | None = None
    prize: str | None = None
    notes: str | None = None
    status: str | None = None
    # Creating only: an existing Map card this competition is being saved
    # from — it gets linked instead of a second card being made later.
    from_card_id: int | None = None


def _clean(payload: CompetitionIn, *, creating: bool) -> dict:
    cleaned = {}
    for key, value in payload.model_dump(exclude_unset=True, exclude={"from_card_id"}).items():
        if isinstance(value, str):
            value = value.strip()
        cleaned[key] = value or None

    if (creating or "title" in cleaned) and not cleaned.get("title"):
        raise HTTPException(status_code=400, detail="Give it a name.")
    for key, limit in _TEXT_LIMITS.items():
        if cleaned.get(key) and len(cleaned[key]) > limit:
            raise HTTPException(status_code=400, detail=f"{_LABELS.get(key, key.capitalize())} is too long.")

    # The link becomes a clickable href, so only web addresses get through:
    # a bare "devpost.com/…" is assumed https, any other scheme is refused.
    url = cleaned.get("url")
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.IGNORECASE):
            raise HTTPException(status_code=400, detail="The link must be a web address (http or https).")
        cleaned["url"] = "https://" + url

    for key, allowed in (("kind", KINDS), ("status", STATUSES)):
        if key in cleaned:
            if cleaned[key] is None:
                del cleaned[key]  # NOT NULL with a default — "unset" keeps it
            elif cleaned[key] not in allowed:
                raise HTTPException(status_code=400, detail=f"Unknown {key}: {cleaned[key]!r}")

    if cleaned.get("deadline"):
        try:
            date.fromisoformat(cleaned["deadline"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Deadline must be a date (YYYY-MM-DD).") from exc
    return cleaned


def _to_json(competition: dict) -> dict:
    data = {key: competition[key] for key in ("id", "created_at", *queries.COMPETITION_FIELDS)}
    stored = competition["screenshot_name"]
    # ?v= changes whenever the screenshot is replaced, so the long-lived
    # cache on the image URL never serves a stale one.
    data["screenshot_url"] = f"/competitions/{competition['id']}/screenshot?v={stored[:8]}" if stored else None
    return data


def _get_or_404(competition_id: int) -> dict:
    competition = queries.get_competition(competition_id)
    if competition is None:
        raise HTTPException(status_code=404, detail="Competition not found")
    return competition


@router.get("/competitions")
def competitions_view(request: Request):
    open_counts = queries.get_competition_open_task_counts()
    competitions = []
    for c in queries.list_competitions():
        data = _to_json(c)
        data["open_tasks"] = open_counts.get(c["id"], 0)
        competitions.append(data)
    return templates.TemplateResponse(
        request,
        "competitions.html",
        {
            "competitions": competitions,
            "today": datetime.now(TZ).date().isoformat(),
            "kinds": KINDS,
            "board_cards": queries.list_unlinked_competition_cards(),
            "pursuits_tab": "competitions",
        },
    )


def _task_json(item: dict) -> dict:
    return {
        "id": item["id"],
        "title": item["title"],
        "status": item["status"],
        "due_date": item["due_date"],
        "brief_date": item["brief_date"],
        "url": f"/items/{item['id']}/open",
        "stamp": timestamps.item_stamp(item),
    }


@router.get("/competitions/{competition_id}")
def competition_page(request: Request, competition_id: int):
    """A competition's own page — its breakdown and tasks; what task
    chips and every other mention of the competition link to."""
    competition = queries.get_competition(competition_id)
    if competition is None:
        return RedirectResponse(url="/competitions", status_code=302)
    return templates.TemplateResponse(
        request,
        "focus.html",
        {"focus": belongs.focus_view(competition=competition), "today": datetime.now(TZ).date().isoformat()},
    )


@router.get("/competitions/{competition_id}/tasks")
def list_tasks(competition_id: int):
    _get_or_404(competition_id)
    return [_task_json(i) for i in queries.list_items_by_competition(competition_id)]


class CompetitionTaskIn(BaseModel):
    title: str


@router.post("/competitions/{competition_id}/tasks")
def add_task(competition_id: int, payload: CompetitionTaskIn):
    """A task for this competition, added to today's brief already linked
    to it (Action items lane — the same neutral default the assistant
    uses when no lane is stated)."""
    _get_or_404(competition_id)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Give the task a name.")
    if len(title) > 300:
        raise HTTPException(status_code=400, detail="Task name is too long.")
    item_id = queries.create_manual_item(datetime.now(TZ).date().isoformat(), "action_items", title)
    queries.set_item_competition(item_id, competition_id)
    return _task_json(queries.get_item(item_id))


@router.post("/competitions")
def create_competition(payload: CompetitionIn):
    fields = _clean(payload, creating=True)
    if payload.from_card_id is not None:
        # Checked before creating anything, so a stale pick (the card was
        # deleted or already saved meanwhile) fails cleanly.
        if not any(c["id"] == payload.from_card_id for c in queries.list_unlinked_competition_cards()):
            raise HTTPException(status_code=400, detail="That Map card is gone or already saved here — pick again.")
    competition_id = queries.create_competition(fields)
    if payload.from_card_id is not None:
        queries.link_map_node_to_competition(payload.from_card_id, competition_id)
    return _to_json(queries.get_competition(competition_id))


@router.patch("/competitions/{competition_id}")
def update_competition(competition_id: int, payload: CompetitionIn):
    _get_or_404(competition_id)
    queries.update_competition(competition_id, _clean(payload, creating=False))
    return _to_json(queries.get_competition(competition_id))


@router.delete("/competitions/{competition_id}")
def delete_competition(competition_id: int):
    competition = _get_or_404(competition_id)
    if competition["screenshot_name"]:
        attachments.delete_stored_file(competition["screenshot_name"])
    queries.delete_competition(competition_id)
    return {"ok": True}


@router.post("/competitions/{competition_id}/screenshot")
async def upload_screenshot(competition_id: int, file: UploadFile = File(...)):
    competition = _get_or_404(competition_id)
    content = await file.read(attachments.MAX_UPLOAD_BYTES + 1)
    try:
        stored_name, content_type, _size = attachments.save_upload(content)
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    queries.set_competition_screenshot(competition_id, stored_name, content_type)
    if competition["screenshot_name"]:
        attachments.delete_stored_file(competition["screenshot_name"])
    return _to_json(queries.get_competition(competition_id))


@router.delete("/competitions/{competition_id}/screenshot")
def remove_screenshot(competition_id: int):
    competition = _get_or_404(competition_id)
    if competition["screenshot_name"]:
        attachments.delete_stored_file(competition["screenshot_name"])
    queries.set_competition_screenshot(competition_id, None, None)
    return _to_json(queries.get_competition(competition_id))


@router.get("/competitions/{competition_id}/screenshot")
def get_screenshot(competition_id: int):
    competition = _get_or_404(competition_id)
    path = attachments.path_for(competition["screenshot_name"] or "")
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="No screenshot")
    return FileResponse(
        path,
        media_type=competition["screenshot_type"],
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.post("/competitions/{competition_id}/to-map")
def send_to_map(competition_id: int):
    competition = _get_or_404(competition_id)
    existing = queries.find_map_node_for_competition(competition_id)
    if existing is not None:
        # Already on the map — one card per competition, so its tasks and
        # details live in one place.
        return {"ok": True, "board_id": existing["board_id"], "node_id": existing["id"]}
    board = queries.get_or_create_default_board()
    details = [d for d in (competition["organizer"], competition["prize"], competition["event_dates"]) if d]
    # Spread new cards out deterministically instead of stacking them all
    # dead-center on the board.
    node_id = queries.create_map_node(
        board["id"],
        _MAP_KIND[competition["kind"]],
        title=competition["title"],
        note=" · ".join(details),
        target_date=competition["deadline"],
        x=12 + (competition_id * 17) % 55,
        y=12 + (competition_id * 29) % 62,
        competition_id=competition_id,
    )
    return {"ok": True, "board_id": board["id"], "node_id": node_id}
