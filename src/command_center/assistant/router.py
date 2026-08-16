"""Assistant routes — POST /assistant/ask (question -> grounded answer),
consumed by the floating chat widget embedded in dashboard.html (no
standalone page — see that template for the widget itself). POST
/settings/assistant/rebuild-index is the manual reindex trigger used by
the Settings page button.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from command_center.assistant import chat, ingest
from command_center.triage import TriageProviderError

logger = logging.getLogger(__name__)

router = APIRouter()


class HistoryTurn(BaseModel):
    role: str
    content: str


class AskIn(BaseModel):
    question: str
    history: list[HistoryTurn] = []


@router.post("/assistant/ask")
def ask(payload: AskIn):
    question = payload.question.strip()
    if not question:
        return JSONResponse({"ok": False, "error": "Ask something first."}, status_code=400)

    history = [turn.model_dump() for turn in payload.history]
    try:
        result = chat.answer(question, history=history)
    except TriageProviderError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    return JSONResponse({"ok": True, **result})


class ConfirmIn(BaseModel):
    pending_action: dict
    confirmed: bool


@router.post("/assistant/confirm")
def confirm(payload: ConfirmIn):
    result = chat.confirm_action(payload.pending_action, payload.confirmed)
    return JSONResponse({"ok": True, **result})


class ConfirmBatchIn(BaseModel):
    tasks: list[dict]
    confirmed: bool


@router.post("/assistant/confirm-batch")
def confirm_batch(payload: ConfirmBatchIn):
    result = chat.confirm_batch(payload.tasks, payload.confirmed)
    return JSONResponse({"ok": True, **result})


@router.post("/settings/assistant/rebuild-index")
def rebuild_index():
    try:
        ingest.rebuild_index(force=True)
    except Exception:
        # Same degrade-not-crash posture as the startup call to this
        # function in app.py's lifespan (a first-run model download can
        # fail offline, for instance) — this is the manually-triggered
        # twin of that same call, so it shouldn't behave differently.
        logger.exception("Manual index rebuild failed")
    return RedirectResponse(url="/settings", status_code=303)
