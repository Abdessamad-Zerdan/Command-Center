from pathlib import Path

import pytest

from command_center import db, queries


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_update_item_from_task_no_op_without_title_or_status(isolated_db: None) -> None:
    assert queries.update_item_from_task("does-not-exist") is False


def test_update_item_from_task_returns_false_when_nothing_matches(isolated_db: None) -> None:
    assert queries.update_item_from_task("does-not-exist", title="X") is False
