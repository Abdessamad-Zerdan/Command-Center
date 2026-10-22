from pathlib import Path

import numpy as np
import pytest

from command_center import db
from command_center.assistant import documents, embeddings, ingest


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(ingest, "DATA_DIR", data_dir)
    monkeypatch.setattr(ingest, "VISION_MD_PATH", data_dir / "vision.md")
    monkeypatch.setattr(documents, "DOCUMENTS_DIR", data_dir / "documents")
    # Deterministic fake embeddings — the real ONNX model never loads in
    # tests (slow, and would need a network download on first use).
    monkeypatch.setattr(
        embeddings,
        "embed_documents",
        lambda texts: np.array([[float(len(t)), 0.0, 0.0] for t in texts]),
    )
    return data_dir


def test_ensure_vision_md_exists_creates_template(isolated: Path) -> None:
    assert not ingest.VISION_MD_PATH.exists()

    ingest.ensure_vision_md_exists()

    assert ingest.VISION_MD_PATH.exists()
    content = ingest.VISION_MD_PATH.read_text(encoding="utf-8")
    for heading in ("## Current Focus", "## Long-term Goals", "## Projects", "## Skills"):
        assert heading in content


def test_ensure_vision_md_exists_does_not_overwrite_existing_content(isolated: Path) -> None:
    ingest.DATA_DIR.mkdir(exist_ok=True)
    ingest.VISION_MD_PATH.write_text(
        "# Vision\n\n## Current Focus\nReal content, not the placeholder.\n", encoding="utf-8"
    )

    ingest.ensure_vision_md_exists()

    assert "Real content, not the placeholder." in ingest.VISION_MD_PATH.read_text(encoding="utf-8")


def test_rebuild_index_skips_when_unchanged(isolated: Path) -> None:
    ingest.rebuild_index()
    with db.session() as conn:
        first_count = conn.execute("SELECT COUNT(*) AS n FROM content_chunks").fetchone()["n"]

    rebuilt_again = ingest.rebuild_index()

    assert rebuilt_again is False
    with db.session() as conn:
        second_count = conn.execute("SELECT COUNT(*) AS n FROM content_chunks").fetchone()["n"]
    assert second_count == first_count
