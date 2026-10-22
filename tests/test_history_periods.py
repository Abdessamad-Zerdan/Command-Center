from datetime import date

import pytest

from command_center.history import periods


def test_parse_anchor_valid_iso_date() -> None:
    assert periods.parse_anchor("2026-08-14") == date(2026, 8, 14)


def test_parse_anchor_none_defaults_to_today() -> None:
    result = periods.parse_anchor(None)
    assert isinstance(result, date)


def test_parse_anchor_invalid_string_raises_value_error() -> None:
    with pytest.raises(ValueError):
        periods.parse_anchor("not-a-date")


def test_period_bounds_day() -> None:
    start, end = periods.period_bounds("day", date(2026, 8, 14))
    assert (start, end) == (date(2026, 8, 14), date(2026, 8, 15))


def test_period_bounds_week_monday_start() -> None:
    # 2026-08-14 is a Friday
    start, end = periods.period_bounds("week", date(2026, 8, 14))
    assert (start, end) == (date(2026, 8, 10), date(2026, 8, 17))


def test_period_bounds_month() -> None:
    start, end = periods.period_bounds("month", date(2026, 8, 14))
    assert (start, end) == (date(2026, 8, 1), date(2026, 9, 1))


def test_period_bounds_month_december_wraps_to_january() -> None:
    start, end = periods.period_bounds("month", date(2026, 12, 5))
    assert (start, end) == (date(2026, 12, 1), date(2027, 1, 1))


def test_period_bounds_month_leap_year_february() -> None:
    start, end = periods.period_bounds("month", date(2028, 2, 10))  # 2028 is a leap year
    assert (start, end) == (date(2028, 2, 1), date(2028, 3, 1))


def test_period_bounds_year() -> None:
    start, end = periods.period_bounds("year", date(2026, 8, 14))
    assert (start, end) == (date(2026, 1, 1), date(2027, 1, 1))


def test_period_bounds_unknown_period_raises_value_error() -> None:
    with pytest.raises(ValueError):
        periods.period_bounds("fortnight", date(2026, 8, 14))


def test_shift_anchor_day() -> None:
    assert periods.shift_anchor("day", date(2026, 8, 14), -1) == date(2026, 8, 13)
    assert periods.shift_anchor("day", date(2026, 8, 14), 1) == date(2026, 8, 15)


def test_shift_anchor_week() -> None:
    assert periods.shift_anchor("week", date(2026, 8, 14), -1) == date(2026, 8, 7)
    assert periods.shift_anchor("week", date(2026, 8, 14), 1) == date(2026, 8, 21)


def test_shift_anchor_month_boundary() -> None:
    # Jan 31 anchor -> previous month lands inside December, next lands inside February
    prev_anchor = periods.shift_anchor("month", date(2026, 1, 31), -1)
    next_anchor = periods.shift_anchor("month", date(2026, 1, 31), 1)
    assert periods.period_bounds("month", prev_anchor) == (date(2025, 12, 1), date(2026, 1, 1))
    assert periods.period_bounds("month", next_anchor) == (date(2026, 2, 1), date(2026, 3, 1))


def test_shift_anchor_year() -> None:
    assert periods.shift_anchor("year", date(2026, 8, 14), -1) == date(2025, 8, 14)
    assert periods.shift_anchor("year", date(2026, 8, 14), 1) == date(2027, 8, 14)


def test_label_day() -> None:
    assert periods.label("day", date(2026, 8, 14), date(2026, 8, 15)) == "Friday, August 14, 2026"


def test_label_week_within_one_month() -> None:
    result = periods.label("week", date(2026, 8, 10), date(2026, 8, 17))
    assert result == "Week of August 10–16, 2026"


def test_label_week_spanning_two_months() -> None:
    result = periods.label("week", date(2026, 8, 31), date(2026, 9, 7))
    assert "Aug" in result and "Sep" in result


def test_label_month() -> None:
    assert periods.label("month", date(2026, 8, 1), date(2026, 9, 1)) == "August 2026"


def test_label_year() -> None:
    assert periods.label("year", date(2026, 1, 1), date(2027, 1, 1)) == "2026"
