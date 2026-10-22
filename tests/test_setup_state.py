from pathlib import Path

import pytest

from command_center.setup_wizard import state as state_module


@pytest.fixture()
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "setup_state.json"
    monkeypatch.setattr(state_module, "STATE_PATH", path)
    return path


def test_load_creates_default_shape_when_missing(isolated_state: Path) -> None:
    assert not isolated_state.exists()
    state = state_module.load()
    assert state["current_step"] == state_module.STEP_ORDER[0]
    assert state["profile"]["name"] == ""
    assert state["credentials"]["groq_api_key"] == ""
    assert state["confirmations"]["medium_skipped"] is False
    assert state["test_results"]["groq"] is None


def test_save_then_load_round_trips(isolated_state: Path) -> None:
    state = state_module.load()
    state["profile"]["name"] = "Test Person"
    state["current_step"] = 7
    state_module.save(state)

    reloaded = state_module.load()
    assert reloaded["profile"]["name"] == "Test Person"
    assert reloaded["current_step"] == 7


def test_next_step_skips_the_reserved_gap() -> None:
    assert state_module.next_step(1) == 2
    assert state_module.next_step(2) == 7  # skips reserved steps 3-6
    assert state_module.next_step(7) == 8
    assert state_module.next_step(8) == 9
    assert state_module.next_step(9) == 10
    assert state_module.next_step(10) is None  # last built step


def test_prev_step_skips_the_reserved_gap() -> None:
    assert state_module.prev_step(1) is None  # no Back on the first step
    assert state_module.prev_step(2) == 1
    assert state_module.prev_step(7) == 2  # skips back over 3-6
    assert state_module.prev_step(8) == 7
    assert state_module.prev_step(10) == 9
