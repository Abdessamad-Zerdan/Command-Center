from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from command_center import db
from command_center.assistant import embeddings, retrieval


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _insert_chunk(source: str, section: str, content: str, vector: list[float]) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO content_chunks (source, section, content, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                source,
                section,
                content,
                np.array(vector, dtype="float32").tobytes(),
                datetime.now().isoformat(),
            ),
        )


def test_top_k_returns_empty_list_when_no_chunks(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "embed_query", lambda q: np.array([1.0, 0.0]))
    assert retrieval.top_k("anything") == []


def test_top_k_ranks_by_cosine_similarity(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_chunk("vision", "Close match", "content a", [1.0, 0.0])
    _insert_chunk("vision", "Orthogonal", "content b", [0.0, 1.0])
    _insert_chunk("vision", "Opposite", "content c", [-1.0, 0.0])
    monkeypatch.setattr(embeddings, "embed_query", lambda q: np.array([1.0, 0.0]))

    results = retrieval.top_k("test query", k=3)

    assert [r["section"] for r in results] == ["Close match", "Orthogonal", "Opposite"]
    assert results[0]["score"] == pytest.approx(1.0)
    assert results[1]["score"] == pytest.approx(0.0, abs=1e-6)
    assert results[2]["score"] == pytest.approx(-1.0)


def test_top_k_respects_k_limit(isolated_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(5):
        _insert_chunk("vision", f"Section {i}", f"content {i}", [1.0, float(i)])
    monkeypatch.setattr(embeddings, "embed_query", lambda q: np.array([1.0, 0.0]))

    assert len(retrieval.top_k("query", k=2)) == 2


def test_top_k_includes_content_and_source_fields(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_chunk("profile", "Bio", "Ada Lovelace — Mathematician.", [1.0, 0.0])
    monkeypatch.setattr(embeddings, "embed_query", lambda q: np.array([1.0, 0.0]))

    results = retrieval.top_k("who are you", k=1)

    assert results[0]["source"] == "profile"
    assert results[0]["section"] == "Bio"
    assert results[0]["content"] == "Ada Lovelace — Mathematician."
