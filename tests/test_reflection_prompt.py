from datetime import date

from command_center.history import reflection


def _completion(by_lane: dict) -> dict:
    total_created = sum(v.get("created", 0) for v in by_lane.values())
    total_completed = sum(v.get("completed", 0) for v in by_lane.values())
    return {"by_lane": by_lane, "total_created": total_created, "total_completed": total_completed}


def _period_data(by_lane: dict, same_day: float = 0.0, avg_time: dict | None = None, rollover: int = 0) -> dict:
    return {
        "completion": _completion(by_lane),
        "same_day": same_day,
        "avg_time": avg_time or {},
        "rollover": rollover,
    }


def test_system_prompt_forbids_speculation_and_caps_length() -> None:
    assert "speculate" in reflection.SYSTEM_PROMPT.lower()
    assert "2 to 4 sentences" in reflection.SYSTEM_PROMPT
    assert "markdown" in reflection.SYSTEM_PROMPT.lower()


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


def test_format_period_block_rounds_hours_and_percentages() -> None:
    data = _period_data(
        {"urgent": {"created": 2, "completed": 1}},
        same_day=0.6666,
        avg_time={"urgent": {"avg_hours": 2.449, "n": 1}},
        rollover=3,
    )

    block = reflection._format_period_block("Current period", data, ["urgent"])

    assert "67%" in block  # round(66.66) == 67
    assert "2.4h" in block
    assert "Rolled over from before this period, still open at period end: 3" in block


def test_format_period_block_shows_no_completions_when_avg_time_empty() -> None:
    data = _period_data({"urgent": {"created": 1, "completed": 0}})

    block = reflection._format_period_block("Current period", data, ["urgent"])

    assert "no completions" in block


def test_build_user_message_all_zero_activity_skips_lane_blocks() -> None:
    current = _period_data({})
    previous = _period_data({})

    message = reflection._build_user_message(
        "week", date(2026, 8, 10), date(2026, 8, 17), date(2026, 8, 3), date(2026, 8, 10), current, previous
    )

    assert "No task activity" in message
    assert "Created:" not in message


def test_build_user_message_includes_current_and_previous_blocks() -> None:
    current = _period_data({"urgent": {"created": 3, "completed": 2}})
    previous = _period_data({"urgent": {"created": 1, "completed": 1}})

    message = reflection._build_user_message(
        "week", date(2026, 8, 10), date(2026, 8, 17), date(2026, 8, 3), date(2026, 8, 10), current, previous
    )

    assert "Current period:" in message
    assert "Previous period:" in message
    assert "Created: 3 total" in message
    assert "Created: 1 total" in message
    assert "Write the reflection now, comparing current to previous." in message
