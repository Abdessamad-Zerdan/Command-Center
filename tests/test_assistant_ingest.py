from pathlib import Path

import numpy as np
import pytest

from _pdf_fixtures import make_test_pdf
from command_center import db, queries
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


def test_chunk_vision_md_splits_on_level_2_headings() -> None:
    text = (
        "# Vision\n\n"
        "## Current Focus\n"
        "Building the daily brief.\n\n"
        "## Long-term Goals\n"
        "Ship something people use daily.\n"
    )
    assert ingest._chunk_vision_md(text) == [
        ("Current Focus", "Building the daily brief."),
        ("Long-term Goals", "Ship something people use daily."),
    ]


def test_chunk_vision_md_skips_empty_sections() -> None:
    text = "## Empty\n\n## Filled\nSome content.\n"
    assert ingest._chunk_vision_md(text) == [("Filled", "Some content.")]


def test_chunk_vision_md_ignores_content_before_first_heading() -> None:
    text = "# Vision\nSome preamble not under any heading.\n\n## Skills\nPython.\n"
    assert ingest._chunk_vision_md(text) == [("Skills", "Python.")]


def test_chunk_profile_includes_bio_and_each_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ingest,
        "PROFILE",
        {"name": "Ada Lovelace", "title": "Mathematician", "tagline": "First algorithm."},
    )
    monkeypatch.setattr(
        ingest,
        "PROJECTS",
        [
            {
                "name": "Analytical Engine",
                "sentence": "Annotated translation.",
                "status_tag": "Shipping",
                "link": None,
            }
        ],
    )

    chunks = ingest._chunk_profile()

    assert chunks[0][0] == "Bio"
    assert "Ada Lovelace" in chunks[0][1]
    assert "Mathematician" in chunks[0][1]
    assert chunks[1] == (
        "Project: Analytical Engine",
        "Analytical Engine (Shipping): Annotated translation.",
    )


def test_rebuild_index_builds_chunks_and_stores_hash(isolated: Path) -> None:
    rebuilt = ingest.rebuild_index()
    assert rebuilt is True

    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM content_chunks").fetchone()["n"]
        assert count > 0
        hash_row = conn.execute(
            "SELECT value FROM index_metadata WHERE key = 'vision_md_hash'"
        ).fetchone()
        assert hash_row is not None


def test_rebuild_index_skips_when_unchanged(isolated: Path) -> None:
    ingest.rebuild_index()
    with db.session() as conn:
        first_count = conn.execute("SELECT COUNT(*) AS n FROM content_chunks").fetchone()["n"]

    rebuilt_again = ingest.rebuild_index()

    assert rebuilt_again is False
    with db.session() as conn:
        second_count = conn.execute("SELECT COUNT(*) AS n FROM content_chunks").fetchone()["n"]
    assert second_count == first_count


def test_rebuild_index_rebuilds_when_vision_md_changes(isolated: Path) -> None:
    ingest.rebuild_index()
    ingest.VISION_MD_PATH.write_text(
        "# Vision\n\n## Current Focus\nSomething entirely new.\n", encoding="utf-8"
    )

    rebuilt = ingest.rebuild_index()

    assert rebuilt is True
    with db.session() as conn:
        rows = conn.execute(
            "SELECT content FROM content_chunks WHERE source = 'vision'"
        ).fetchall()
    assert any("Something entirely new." in r["content"] for r in rows)


def test_rebuild_index_force_rebuilds_even_when_unchanged(isolated: Path) -> None:
    ingest.rebuild_index()
    assert ingest.rebuild_index(force=True) is True


def test_get_index_status_reports_count_and_timestamp(isolated: Path) -> None:
    ingest.rebuild_index()
    status = ingest.get_index_status()
    assert status["chunk_count"] > 0
    assert status["last_indexed_at"] is not None


# --- knowledge documents ---------------------------------------------------------


def test_chunk_documents_extracts_and_labels_uploaded_pdfs(isolated: Path) -> None:
    stored_name, _, char_count = documents.save_upload("resume.pdf", make_test_pdf("Ada CV text"))
    queries.create_knowledge_document("resume.pdf", stored_name, char_count)

    sections = ingest._chunk_documents()

    assert sections == [("resume.pdf", "Ada CV text")]


def test_chunk_documents_labels_multi_chunk_documents_with_part_numbers(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored_name, _, char_count = documents.save_upload("resume.pdf", make_test_pdf("Some text"))
    queries.create_knowledge_document("resume.pdf", stored_name, char_count)
    monkeypatch.setattr(documents, "chunk_document_text", lambda text: ["Part one", "Part two"])

    sections = ingest._chunk_documents()

    assert sections == [("resume.pdf (part 1)", "Part one"), ("resume.pdf (part 2)", "Part two")]


def test_chunk_documents_skips_a_missing_file_without_raising(isolated: Path) -> None:
    queries.create_knowledge_document("ghost.pdf", "does-not-exist.pdf", 100)
    assert ingest._chunk_documents() == []


def test_rebuild_index_includes_uploaded_document_chunks(isolated: Path) -> None:
    stored_name, _, char_count = documents.save_upload(
        "resume.pdf", make_test_pdf("Backend engineer with 8 years experience")
    )
    queries.create_knowledge_document("resume.pdf", stored_name, char_count)

    ingest.rebuild_index()

    with db.session() as conn:
        rows = conn.execute(
            "SELECT section, content FROM content_chunks WHERE source = 'document'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["section"] == "resume.pdf"
    assert "Backend engineer" in rows[0]["content"]


def test_rebuild_index_still_includes_vision_and_profile_alongside_documents(
    isolated: Path,
) -> None:
    stored_name, _, char_count = documents.save_upload("resume.pdf", make_test_pdf("CV text"))
    queries.create_knowledge_document("resume.pdf", stored_name, char_count)

    ingest.rebuild_index()

    with db.session() as conn:
        sources = {
            row["source"]
            for row in conn.execute("SELECT DISTINCT source FROM content_chunks").fetchall()
        }
    assert {"vision", "profile", "document"} <= sources
