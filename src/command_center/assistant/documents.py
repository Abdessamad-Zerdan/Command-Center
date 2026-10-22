"""PDF text extraction + chunking for uploaded CVs/project docs — the
assistant's corpus grows to include these alongside vision.md and
profile.py (see ingest.py). Storage is flat files on disk
(data/documents/, gitignored) with metadata in the knowledge_documents
table; re-extracted from the stored PDF on every index rebuild, same
"re-read from source each time" approach ingest.py already uses for
vision.md — simple, and cheap enough at personal-document volume.
"""

import re
import uuid
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from command_center.config import REPO_ROOT

DOCUMENTS_DIR = REPO_ROOT / "data" / "documents"

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB — generous for a CV/project doc, not a book
_CHUNK_TARGET_CHARS = 1500


class DocumentError(Exception):
    """Raised for a rejected upload (wrong type, too large, unreadable) —
    always with a message safe to show the user directly."""


def _new_stored_name() -> str:
    return f"{uuid.uuid4().hex}.pdf"


def save_upload(filename: str, content: bytes) -> tuple[str, str, int]:
    """Validates, extracts, and stores one uploaded PDF. Returns
    (stored_name, extracted_text, char_count). Raises DocumentError for
    anything that shouldn't reach the database — extraction happens
    *before* the file is written to disk, so a corrupt/non-PDF upload
    never leaves an orphaned file behind.
    """
    if not filename.lower().endswith(".pdf"):
        raise DocumentError("Only PDF files are supported right now.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise DocumentError(
            f"File is too large ({len(content) / 1_048_576:.1f} MB) — "
            f"the limit is {MAX_UPLOAD_BYTES / 1_048_576:.0f} MB."
        )

    text = extract_text(content)
    if not text.strip():
        raise DocumentError(
            "Couldn't find any text in this PDF — it may be a scanned image "
            "without a text layer, which isn't supported yet."
        )

    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = _new_stored_name()
    (DOCUMENTS_DIR / stored_name).write_bytes(content)
    return stored_name, text, len(text)


def extract_text(content: bytes) -> str:
    import io

    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise DocumentError(f"Couldn't read this PDF — it may be corrupted ({exc}).") from exc
    return "\n\n".join(p.strip() for p in pages if p.strip())


def extract_text_from_file(path: Path) -> str:
    return extract_text(path.read_bytes())


def delete_stored_file(stored_name: str) -> None:
    path = DOCUMENTS_DIR / stored_name
    if path.exists():
        path.unlink()


def chunk_document_text(text: str) -> list[str]:
    """Greedily groups paragraphs into ~_CHUNK_TARGET_CHARS-sized chunks —
    unlike vision.md's '## heading'-based sections, an arbitrary CV/PDF
    has no reliable heading structure to split on, so this is a size
    budget instead. A single paragraph longer than the target still
    becomes its own whole chunk rather than being cut mid-sentence.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if current and current_len + len(para) > _CHUNK_TARGET_CHARS:
            chunks.append("\n\n".join(current))
            current, current_len = [], 0
        current.append(para)
        current_len += len(para)
    if current:
        chunks.append("\n\n".join(current))
    return chunks
