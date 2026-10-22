"""Photos attached to brief items. Flat files on disk (data/attachments/,
gitignored) with metadata in the item_attachments table — the same
storage split documents.py uses for knowledge uploads.

The image type is decided from the file's own bytes, never from its
filename or the browser-declared content type, and stored names are
random — nothing the uploader supplies ever becomes part of a path.
Only formats every browser can display are accepted; SVG in particular
is not, since it can carry script.
"""

import re
import uuid
from pathlib import Path

from command_center.config import REPO_ROOT

ATTACHMENTS_DIR = REPO_ROOT / "data" / "attachments"

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB — a full-resolution phone photo, with room to spare

_STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|gif|webp)$")


class AttachmentError(Exception):
    """A rejected upload — always with a message safe to show the user."""


def sniff_image_type(content: bytes) -> tuple[str, str] | None:
    """(extension, content_type) for JPEG/PNG/GIF/WebP, judged by magic
    bytes; None for anything else."""
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def save_upload(content: bytes) -> tuple[str, str, int]:
    """Validates and stores one uploaded photo. Returns (stored_name,
    content_type, size_bytes). Validation happens before anything is
    written, so a rejected upload never leaves a file behind."""
    if not content:
        raise AttachmentError("That file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise AttachmentError(
            f"Photo is too large ({len(content) / 1_048_576:.1f} MB) — "
            f"the limit is {MAX_UPLOAD_BYTES / 1_048_576:.0f} MB."
        )
    kind = sniff_image_type(content)
    if kind is None:
        raise AttachmentError("Only photos are supported — JPEG, PNG, GIF or WebP.")
    extension, content_type = kind

    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}.{extension}"
    (ATTACHMENTS_DIR / stored_name).write_bytes(content)
    return stored_name, content_type, len(content)


def path_for(stored_name: str) -> Path | None:
    """The on-disk path for a stored name — None unless the name is
    exactly the shape save_upload generates."""
    if not _STORED_NAME_RE.match(stored_name):
        return None
    return ATTACHMENTS_DIR / stored_name


def delete_stored_file(stored_name: str) -> None:
    path = path_for(stored_name)
    if path is not None and path.exists():
        path.unlink()
