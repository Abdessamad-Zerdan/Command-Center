from pathlib import Path

import pytest

from command_center import db, queries


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_was_nudge_notified_today_false_before_any_send(isolated_db: None) -> None:
    assert queries.was_nudge_notified_today("stale_urgent", "2026-08-17") is False


def test_mark_nudge_notified_then_was_notified_today_true(isolated_db: None) -> None:
    queries.mark_nudge_notified("stale_urgent", "2026-08-17")
    assert queries.was_nudge_notified_today("stale_urgent", "2026-08-17") is True


def test_was_nudge_notified_today_false_for_a_different_day(isolated_db: None) -> None:
    queries.mark_nudge_notified("stale_urgent", "2026-08-17")
    assert queries.was_nudge_notified_today("stale_urgent", "2026-08-18") is False


def test_nudge_ids_are_tracked_independently(isolated_db: None) -> None:
    queries.mark_nudge_notified("stale_urgent", "2026-08-17")
    assert queries.was_nudge_notified_today("overdue_tasks", "2026-08-17") is False


def test_mark_nudge_notified_is_idempotent(isolated_db: None) -> None:
    queries.mark_nudge_notified("stale_urgent", "2026-08-17")
    queries.mark_nudge_notified("stale_urgent", "2026-08-17")
    assert queries.was_nudge_notified_today("stale_urgent", "2026-08-17") is True
