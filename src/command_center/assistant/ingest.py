"""Chunking + index build for the assistant's corpus: data/vision.md
(split by ## heading) and profile.py's PROFILE/PROJECTS (one Bio chunk,
one per project). Task/brief data is deliberately NOT ingested here — it
changes too often for this hash-based reindex model; chat.py fetches it
live at question time instead (see chat.py's docstring).
"""

import hashlib
from datetime import datetime

from command_center import db
from command_center.assistant import embeddings
from command_center.config import PROFILE, PROJECTS, REPO_ROOT, TZ

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


def _chunk_vision_md(text: str) -> list[tuple[str, str]]:
    """Splits on '## ' headings — heading text is the section label, body
    until the next heading (or EOF) is the content. The '# Vision' title
    line and anything before the first '## ' aren't a section themselves.
    """
    sections: list[tuple[str, str]] = []
    heading: str | None = None
    body_lines: list[str] = []

    def _flush() -> None:
        if heading is not None:
            content = "\n".join(body_lines).strip()
            if content:
                sections.append((heading, content))

    for line in text.splitlines():
        if line.startswith("## "):
            _flush()
            heading = line[3:].strip()
            body_lines = []
        elif heading is not None:
            body_lines.append(line)
    _flush()
    return sections


def _chunk_profile() -> list[tuple[str, str]]:
    chunks = [
        ("Bio", f"{PROFILE['name']} — {PROFILE['title']}. {PROFILE['tagline']}"),
    ]
    for project in PROJECTS:
        content = f"{project['name']} ({project['status_tag']}): {project['sentence']}"
        chunks.append((f"Project: {project['name']}", content))
    return chunks


def _get_metadata(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM index_metadata WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_metadata(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO index_metadata (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def rebuild_index(force: bool = False) -> bool:
    """Returns True if the index was actually rebuilt, False if skipped
    because vision.md is unchanged and chunks already exist.
    """
    ensure_vision_md_exists()
    current_hash = _vision_md_hash()

    with db.session() as conn:
        stored_hash = _get_metadata(conn, "vision_md_hash")
        chunk_count = conn.execute("SELECT COUNT(*) AS n FROM content_chunks").fetchone()["n"]

        if not force and stored_hash == current_hash and chunk_count > 0:
            return False

        all_chunks = [
            ("vision", section, content)
            for section, content in _chunk_vision_md(VISION_MD_PATH.read_text(encoding="utf-8"))
        ] + [("profile", section, content) for section, content in _chunk_profile()]

        conn.execute("DELETE FROM content_chunks")

        if all_chunks:
            vectors = embeddings.embed_documents([content for _, _, content in all_chunks])
            now = datetime.now(TZ).isoformat()
            for (source, section, content), vector in zip(all_chunks, vectors):
                conn.execute(
                    "INSERT INTO content_chunks (source, section, content, embedding, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (source, section, content, vector.astype("float32").tobytes(), now),
                )

        _set_metadata(conn, "vision_md_hash", current_hash)
        _set_metadata(conn, "last_indexed_at", datetime.now(TZ).isoformat())

    return True


def get_index_status() -> dict:
    with db.session() as conn:
        last_indexed_at = _get_metadata(conn, "last_indexed_at")
        chunk_count = conn.execute("SELECT COUNT(*) AS n FROM content_chunks").fetchone()["n"]
    return {"last_indexed_at": last_indexed_at, "chunk_count": chunk_count}
