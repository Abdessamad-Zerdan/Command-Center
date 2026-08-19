from pathlib import Path

import pytest

from command_center.assistant import documents
from _pdf_fixtures import make_corrupt_pdf, make_test_pdf


@pytest.fixture()
def isolated_documents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    docs_dir = tmp_path / "documents"
    monkeypatch.setattr(documents, "DOCUMENTS_DIR", docs_dir)
    return docs_dir


# --- extract_text -------------------------------------------------------------


def test_extract_text_returns_the_real_pdf_content() -> None:
    pdf_bytes = make_test_pdf("Senior widget engineer, 10 years experience")
    assert documents.extract_text(pdf_bytes) == "Senior widget engineer, 10 years experience"


def test_extract_text_raises_document_error_for_corrupt_pdf() -> None:
    with pytest.raises(documents.DocumentError):
        documents.extract_text(make_corrupt_pdf())


# --- save_upload ----------------------------------------------------------------


def test_save_upload_rejects_non_pdf_filename(isolated_documents_dir: Path) -> None:
    with pytest.raises(documents.DocumentError, match="PDF"):
        documents.save_upload("resume.docx", make_test_pdf("hi"))


def test_save_upload_rejects_content_over_the_size_limit(isolated_documents_dir: Path) -> None:
    oversized = b"x" * (documents.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(documents.DocumentError, match="too large"):
        documents.save_upload("resume.pdf", oversized)


def test_save_upload_rejects_a_pdf_with_no_extractable_text(isolated_documents_dir: Path) -> None:
    blank_pdf = make_test_pdf("")
    with pytest.raises(documents.DocumentError, match="text"):
        documents.save_upload("scanned.pdf", blank_pdf)


def test_save_upload_rejects_a_corrupt_pdf(isolated_documents_dir: Path) -> None:
    with pytest.raises(documents.DocumentError):
        documents.save_upload("broken.pdf", make_corrupt_pdf())


def test_save_upload_writes_the_file_and_returns_metadata(isolated_documents_dir: Path) -> None:
    stored_name, text, char_count = documents.save_upload("resume.pdf", make_test_pdf("Real CV text"))

    assert stored_name.endswith(".pdf")
    assert (isolated_documents_dir / stored_name).exists()
    assert text == "Real CV text"
    assert char_count == len("Real CV text")


def test_save_upload_does_not_leave_an_orphaned_file_on_rejection(
    isolated_documents_dir: Path,
) -> None:
    with pytest.raises(documents.DocumentError):
        documents.save_upload("broken.pdf", make_corrupt_pdf())

    assert not isolated_documents_dir.exists() or list(isolated_documents_dir.iterdir()) == []


def test_save_upload_generates_unique_stored_names(isolated_documents_dir: Path) -> None:
    name1, _, _ = documents.save_upload("a.pdf", make_test_pdf("A"))
    name2, _, _ = documents.save_upload("b.pdf", make_test_pdf("B"))
    assert name1 != name2


# --- extract_text_from_file / delete_stored_file --------------------------------


def test_extract_text_from_file_reads_a_stored_document(isolated_documents_dir: Path) -> None:
    stored_name, _, _ = documents.save_upload("resume.pdf", make_test_pdf("On-disk content"))
    text = documents.extract_text_from_file(documents.DOCUMENTS_DIR / stored_name)
    assert text == "On-disk content"


def test_delete_stored_file_removes_an_existing_file(isolated_documents_dir: Path) -> None:
    stored_name, _, _ = documents.save_upload("resume.pdf", make_test_pdf("X"))
    documents.delete_stored_file(stored_name)
    assert not (isolated_documents_dir / stored_name).exists()


def test_delete_stored_file_is_a_no_op_for_a_missing_file(isolated_documents_dir: Path) -> None:
    documents.delete_stored_file("does-not-exist.pdf")  # must not raise


# --- chunk_document_text ---------------------------------------------------------


def test_chunk_document_text_groups_paragraphs_under_the_budget() -> None:
    p1, p2, p3 = "a" * 600, "b" * 600, "c" * 600
    text = f"{p1}\n\n{p2}\n\n{p3}"

    chunks = documents.chunk_document_text(text)

    assert len(chunks) == 2
    assert chunks[0] == f"{p1}\n\n{p2}"
    assert chunks[1] == p3


def test_chunk_document_text_keeps_an_oversized_paragraph_whole() -> None:
    huge = "x" * 2000
    chunks = documents.chunk_document_text(huge)
    assert chunks == [huge]


def test_chunk_document_text_ignores_blank_paragraphs() -> None:
    text = "First.\n\n\n\n   \n\nSecond."
    assert documents.chunk_document_text(text) == ["First.\n\nSecond."]


def test_chunk_document_text_empty_input_returns_no_chunks() -> None:
    assert documents.chunk_document_text("") == []
