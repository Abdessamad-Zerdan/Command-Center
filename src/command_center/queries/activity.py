"""Assistant tool-call log, task event log, knowledge documents.

Part of the queries package — see queries/__init__.py."""

import json
from datetime import datetime
from typing import Any

from command_center.config import TZ
from command_center.db import session


def log_tool_call(tool: str, args: dict[str, Any], status: str) -> None:
    """Append-only — each event (proposed, then confirmed/cancelled/failed)
    is its own row, so the full attempt history stays visible for
    debugging rather than being overwritten in place."""
    with session() as conn:
        conn.execute(
            "INSERT INTO tool_call_log (tool, args_json, status, created_at) "
            "VALUES (?, ?, ?, ?)",
            (tool, json.dumps(args), status, datetime.now(TZ).isoformat()),
        )



def list_tool_calls(limit: int = 50) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM tool_call_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]



def list_task_events(limit: int = 50) -> list[dict[str, Any]]:
    """task_id is a soft reference (see db.py's SCHEMA) — the row it
    pointed at may since have been mutated, replaced, or deleted, so
    metadata_json (captured at the moment of the event) is the only
    reliable source for what it was about, not a join back to items."""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM task_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]



# --- knowledge_documents -------------------------------------------------------
# Uploaded CVs/project docs the assistant's index draws from, alongside
# vision.md and profile.py — see assistant/documents.py for extraction.


def create_knowledge_document(filename: str, stored_name: str, char_count: int) -> int:
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO knowledge_documents (filename, stored_name, char_count, uploaded_at) "
            "VALUES (?, ?, ?, ?)",
            (filename, stored_name, char_count, datetime.now(TZ).isoformat()),
        )
        return cursor.lastrowid



def list_knowledge_documents() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM knowledge_documents ORDER BY uploaded_at DESC, id DESC"
        ).fetchall()
        return [dict(row) for row in rows]



def get_knowledge_document(doc_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM knowledge_documents WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None



def delete_knowledge_document(doc_id: int) -> bool:
    with session() as conn:
        cursor = conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (doc_id,))
        return cursor.rowcount > 0
