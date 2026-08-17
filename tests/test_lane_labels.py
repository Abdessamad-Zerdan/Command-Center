from pathlib import Path

import pytest

from command_center import db, queries
from command_center.config import LANE_LABELS


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_get_lane_labels_returns_config_defaults_with_no_overrides(isolated_db: None) -> None:
    assert queries.get_lane_labels() == dict(LANE_LABELS)


def test_set_lane_label_overrides_the_default(isolated_db: None) -> None:
    queries.set_lane_label("urgent", "Fires")

    labels = queries.get_lane_labels()
    assert labels["urgent"] == "Fires"
    # Every other lane stays at its config default, untouched.
    for lane, label in LANE_LABELS.items():
        if lane != "urgent":
            assert labels[lane] == label


def test_set_lane_label_rejects_an_unknown_lane(isolated_db: None) -> None:
    assert queries.set_lane_label("not_a_lane", "Whatever") is False
    assert queries.get_lane_labels() == dict(LANE_LABELS)


def test_set_lane_label_blank_clears_the_override(isolated_db: None) -> None:
    queries.set_lane_label("urgent", "Fires")
    queries.set_lane_label("urgent", "   ")

    assert queries.get_lane_labels()["urgent"] == LANE_LABELS["urgent"]


def test_set_lane_label_strips_whitespace(isolated_db: None) -> None:
    queries.set_lane_label("reading", "  Later  ")
    assert queries.get_lane_labels()["reading"] == "Later"


def test_set_lane_label_persists_across_repeated_calls(isolated_db: None) -> None:
    queries.set_lane_label("action_items", "To Do")
    queries.set_lane_label("action_items", "Doing")

    assert queries.get_lane_labels()["action_items"] == "Doing"
