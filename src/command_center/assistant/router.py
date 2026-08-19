"""Assistant routes — POST /assistant/ask (question -> grounded answer),
consumed by the floating chat widget embedded in dashboard.html (no
standalone page — see that template for the widget itself). POST
/settings/assistant/rebuild-index is the manual reindex trigger used by
the Settings page button.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from command_center import queries
from command_center.assistant import chat, documents, ingest
from command_center.triage import TriageProviderError

logger = logging.getLogger(__name__)

router = APIRouter()

# A fresh Jinja2Templates instance rather than importing app.py's — app.py
# needs to import this router, so importing back from here would be
# circular. Same directory, same plain (no custom filters) setup as
# app.py's own instance.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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


@router.get("/settings/knowledge")
def knowledge_page(request: Request):
    return templates.TemplateResponse(
        request, "settings_knowledge.html", {"documents": queries.list_knowledge_documents()}
    )


@router.post("/settings/knowledge/upload")
async def upload_knowledge_document(file: UploadFile = File(...)):
    content = await file.read()
    try:
        stored_name, _text, char_count = documents.save_upload(file.filename or "upload.pdf", content)
    except documents.DocumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    doc_id = queries.create_knowledge_document(file.filename or "upload.pdf", stored_name, char_count)
    try:
        ingest.rebuild_index(force=True)
    except Exception:
        # The upload itself succeeded — a reindex failure (e.g. embedding
        # model unavailable) shouldn't undo it or read as an upload
        # failure. Next successful rebuild (manual or automatic) picks
        # this document up.
        logger.exception("Reindex after knowledge document upload failed")

    return JSONResponse({"ok": True, "id": doc_id, "char_count": char_count})


@router.delete("/settings/knowledge/{doc_id}")
def delete_knowledge_document(doc_id: int):
    doc = queries.get_knowledge_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    documents.delete_stored_file(doc["stored_name"])
    queries.delete_knowledge_document(doc_id)
    try:
        ingest.rebuild_index(force=True)
    except Exception:
        logger.exception("Reindex after knowledge document deletion failed")

    return {"ok": True}
