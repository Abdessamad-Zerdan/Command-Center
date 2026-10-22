"""Chunking + index build for the assistant's corpus: data/vision.md
(split by ## heading), profile.py's PROFILE/PROJECTS (one Bio chunk,
one per project), and any uploaded PDF documents (see documents.py —
size-budgeted chunks, no heading structure to split on). Task/brief
data is deliberately NOT ingested here — it changes too often for this
hash-based reindex model; chat.py fetches it live at question time
instead (see chat.py's docstring).
"""

import hashlib
import logging
import re

from command_center import db, queries
from command_center.assistant import documents
from command_center.config import PROFILE, PROJECTS, REPO_ROOT
from command_center.triage import TriageProviderError

logger = logging.getLogger(__name__)

DATA_DIR = REPO_ROOT / "data"
VISION_MD_PATH = DATA_DIR / "vision.md"

VISION_MD_TEMPLATE = """# Vision

## Current Focus
(What are you working on right now?)

## Long-term Goals
(Where do you want this to go?)

## Projects
(Notable projects and what they're for — beyond the portfolio list.)

## Skills
(Technical and domain skills worth the assistant knowing about.)
"""


def ensure_vision_md_exists() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    if not VISION_MD_PATH.exists():
        VISION_MD_PATH.write_text(VISION_MD_TEMPLATE, encoding="utf-8")


def _vision_md_hash() -> str:
    if not VISION_MD_PATH.exists():
        return ""
    return hashlib.sha256(VISION_MD_PATH.read_bytes()).hexdigest()


def _corpus_hash() -> str:
    """Fingerprint of everything the index is built from — vision.md, the
    profile, and the uploaded documents. Hashing vision.md alone missed
    profile.py edits: an index built from profile_example (before
    profile.py existed) kept answering with "Your Name" / "Example
    Project" for weeks, since vision.md itself never changed."""
    digest = hashlib.sha256()
    digest.update(_vision_md_hash().encode())
    for section, content in _chunk_profile():
        digest.update(f"{section}\n{content}\n".encode())
    for doc in queries.list_knowledge_documents():
        digest.update(f"{doc['stored_name']}\n".encode())
    return digest.hexdigest()


# A template section left as it came — e.g. "(What are you working on
# right now?)" — is a question to the user, not a fact about them.


def _chunk_vision_md(text: str) -> list[tuple[str, str]]:
    """Splits on '## ' headings — heading text is the section label, body
    until the next heading (or EOF) is the content. The '# Vision' title
    line and anything before the first '## ' aren't a section themselves.
    """
    raise TriageProviderError("_chunk_vision_md: withheld from the public showcase - see README.md")


def _chunk_profile() -> list[tuple[str, str]]:
    raise TriageProviderError("_chunk_profile: withheld from the public showcase - see README.md")


def _chunk_documents() -> list[tuple[str, str]]:
    """Re-extracts every uploaded document from disk on each rebuild —
    same "re-read from source" approach as vision.md, just with a PDF
    instead of a markdown file. One document failing to re-extract
    (deleted file, corrupted PDF) is logged and skipped, not fatal to
    the rest of the index — matches every other degrade-don't-crash path
    in this app.
    """
    raise TriageProviderError("_chunk_documents: withheld from the public showcase - see README.md")


def _get_metadata(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM index_metadata WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def rebuild_index(force: bool = False) -> bool:
    """Returns True if the index was actually rebuilt, False if skipped
    because nothing it's built from changed (see _corpus_hash).
    """
    return False  # withheld: no index is built


def get_index_status() -> dict:
    with db.session() as conn:
        last_indexed_at = _get_metadata(conn, "last_indexed_at")
        chunk_count = conn.execute("SELECT COUNT(*) AS n FROM content_chunks").fetchone()["n"]
    return {"last_indexed_at": last_indexed_at, "chunk_count": chunk_count}
