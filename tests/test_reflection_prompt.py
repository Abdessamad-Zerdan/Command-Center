from pathlib import Path

import pytest

from command_center import db
from command_center.history import reflection


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # _format_period_block reads queries.get_lane_labels() (renamed lanes
    # since Settings > Lane labels) — needs an isolated DB, not whatever
    # command_center.db happens to be on the machine running these tests.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _completion(by_lane: dict) -> dict:
    total_created = sum(v.get("created", 0) for v in by_lane.values())
    total_completed = sum(v.get("completed", 0) for v in by_lane.values())
    return {"by_lane": by_lane, "total_created": total_created, "total_completed": total_completed}


def test_lane_order_includes_lanes_present_in_either_period_only() -> None:
    current = _completion({"urgent": {"created": 2, "completed": 1}})
    previous = _completion({"reading": {"created": 1, "completed": 1}})

    lanes = reflection._lane_order(current, previous)

    assert lanes == ["urgent", "reading"]


def test_lane_order_drops_lanes_absent_from_both_periods() -> None:
    current = _completion({"urgent": {"created": 1, "completed": 0}})
    previous = _completion({"urgent": {"created": 1, "completed": 0}})

    lanes = reflection._lane_order(current, previous)

    assert "action_items" not in lanes
    assert "meeting_prep" not in lanes


def test_lane_order_puts_unknown_last() -> None:
    current = _completion({"unknown": {"created": 1, "completed": 0}, "urgent": {"created": 1, "completed": 0}})
    previous = _completion({})

    lanes = reflection._lane_order(current, previous)

    assert lanes[-1] == "unknown"
    assert lanes[0] == "urgent"
