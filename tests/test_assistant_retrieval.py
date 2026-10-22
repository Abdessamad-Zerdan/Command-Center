from pathlib import Path

import numpy as np
import pytest

from command_center import db
from command_center.assistant import embeddings, retrieval


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_top_k_returns_empty_list_when_no_chunks(
    isolated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "embed_query", lambda q: np.array([1.0, 0.0]))
    assert retrieval.top_k("anything") == []
