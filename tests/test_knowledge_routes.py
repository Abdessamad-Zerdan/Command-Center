from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _pdf_fixtures import make_corrupt_pdf, make_test_pdf
from command_center import auth, db, queries
from command_center.assistant import documents
from command_center.assistant import ingest as assistant_ingest
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(documents, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    # rebuild_index still runs for real here (not stubbed to a no-op like
    # most other client fixtures in this suite) — these tests specifically
    # need to see it get triggered by upload/delete, just with a fake
    # embedding model instead of the real ONNX one.
    monkeypatch.setattr(assistant_ingest, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(assistant_ingest, "VISION_MD_PATH", tmp_path / "data" / "vision.md")

    import numpy as np

    from command_center.assistant import embeddings

    monkeypatch.setattr(
        embeddings,
        "embed_documents",
        lambda texts: np.array([[float(len(t)), 0.0, 0.0] for t in texts]),
    )

    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def _upload(client: TestClient, filename: str, content: bytes):
    return client.post(
        "/settings/knowledge/upload",
        files={"file": (filename, content, "application/pdf")},
    )


def test_knowledge_page_renders_empty_state(client: TestClient) -> None:
    response = client.get("/settings/knowledge")
    assert response.status_code == 200
    assert "No documents uploaded yet." in response.text


def test_settings_page_links_to_knowledge(client: TestClient) -> None:
    response = client.get("/settings")
    assert 'href="/settings/knowledge"' in response.text


def test_upload_a_valid_pdf_succeeds(client: TestClient) -> None:
    response = _upload(client, "resume.pdf", make_test_pdf("Backend engineer, 8 years"))

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["char_count"] == len("Backend engineer, 8 years")

    docs = queries.list_knowledge_documents()
    assert len(docs) == 1
    assert docs[0]["filename"] == "resume.pdf"


def test_upload_stores_the_file_on_disk(client: TestClient) -> None:
    _upload(client, "resume.pdf", make_test_pdf("On disk check"))
    doc = queries.list_knowledge_documents()[0]
    assert (documents.DOCUMENTS_DIR / doc["stored_name"]).exists()


def test_upload_triggers_a_reindex_that_includes_the_document(client: TestClient) -> None:
    _upload(client, "resume.pdf", make_test_pdf("Reindex check content"))

    with db.session() as conn:
        rows = conn.execute(
            "SELECT content FROM content_chunks WHERE source = 'document'"
        ).fetchall()
    assert any("Reindex check content" in r["content"] for r in rows)


def test_upload_rejects_a_non_pdf_file(client: TestClient) -> None:
    response = _upload(client, "resume.docx", b"not a pdf")
    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]
    assert queries.list_knowledge_documents() == []


def test_upload_rejects_a_corrupt_pdf(client: TestClient) -> None:
    response = _upload(client, "broken.pdf", make_corrupt_pdf())
    assert response.status_code == 400
    assert queries.list_knowledge_documents() == []


def test_knowledge_page_lists_an_uploaded_document(client: TestClient) -> None:
    _upload(client, "resume.pdf", make_test_pdf("Listed content"))

    response = client.get("/settings/knowledge")

    assert "resume.pdf" in response.text


def test_delete_knowledge_document_removes_it(client: TestClient) -> None:
    _upload(client, "resume.pdf", make_test_pdf("To be deleted"))
    doc_id = queries.list_knowledge_documents()[0]["id"]

    response = client.delete(f"/settings/knowledge/{doc_id}")

    assert response.status_code == 200
    assert queries.list_knowledge_documents() == []


def test_delete_knowledge_document_removes_the_file_from_disk(client: TestClient) -> None:
    _upload(client, "resume.pdf", make_test_pdf("File check"))
    doc = queries.list_knowledge_documents()[0]
    stored_path = documents.DOCUMENTS_DIR / doc["stored_name"]
    assert stored_path.exists()

    client.delete(f"/settings/knowledge/{doc['id']}")

    assert not stored_path.exists()


def test_delete_knowledge_document_reindexes_without_it(client: TestClient) -> None:
    _upload(client, "resume.pdf", make_test_pdf("Will be removed"))
    doc_id = queries.list_knowledge_documents()[0]["id"]

    client.delete(f"/settings/knowledge/{doc_id}")

    with db.session() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM content_chunks WHERE source = 'document'").fetchone()
    assert rows["n"] == 0


def test_delete_knowledge_document_404s_for_unknown_id(client: TestClient) -> None:
    response = client.delete("/settings/knowledge/99999")
    assert response.status_code == 404
