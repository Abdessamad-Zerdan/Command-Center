from datetime import datetime, timedelta
from pathlib import Path

import pytest

from command_center import db, nudges, queries
from command_center.config import TZ


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _seed_item(
    brief_date: str, lane: str, source: str, source_id: str, title: str,
    created_at: str, due_date: str | None = None,
) -> int:
    with db.session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO briefs (brief_date, generated_at, degraded_lanes) "
            "VALUES (?, ?, '[]')",
            (brief_date, created_at),
        )
        cursor = conn.execute(
            "INSERT INTO items (brief_date, lane, source, source_id, title, why_it_matters, "
            "suggested_next_step, priority, deep_link, due_date, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, '', '', 2, '', ?, 'pending', ?)",
            (brief_date, lane, source, source_id, title, due_date, created_at),
        )
        return cursor.lastrowid


def test_compute_nudges_empty_when_nothing_stale_or_overdue(isolated_db: None) -> None:
    now = datetime(2026, 8, 17, 9, 0, tzinfo=TZ)
    assert nudges.compute_nudges("2026-08-17", now=now) == []


def test_stale_urgent_nudge_fires_after_two_days(isolated_db: None) -> None:
    now = datetime(2026, 8, 17, 9, 0, tzinfo=TZ)
    old = (now - timedelta(days=3)).isoformat()
    item_id = _seed_item("2026-08-14", "urgent", "gmail", "g1", "Old alert", created_at=old)

    result = nudges.compute_nudges("2026-08-17", now=now)

    assert len(result) == 1
    assert result[0]["id"] == "stale_urgent"
    assert "Old alert" in result[0]["message"]
    assert result[0]["targets"] == [{"id": item_id}]


def test_stale_urgent_nudge_does_not_fire_for_recent_items(isolated_db: None) -> None:
    now = datetime(2026, 8, 17, 9, 0, tzinfo=TZ)
    recent = (now - timedelta(hours=1)).isoformat()
    _seed_item("2026-08-17", "urgent", "gmail", "g2", "Fresh alert", created_at=recent)

    assert nudges.compute_nudges("2026-08-17", now=now) == []


def test_stale_urgent_nudge_ignores_non_urgent_lanes(isolated_db: None) -> None:
    now = datetime(2026, 8, 17, 9, 0, tzinfo=TZ)
    old = (now - timedelta(days=5)).isoformat()
    _seed_item("2026-08-14", "action_items", "gmail", "g3", "Old but not urgent", created_at=old)

    assert nudges.compute_nudges("2026-08-17", now=now) == []


def test_overdue_task_nudge_fires_for_a_past_due_date(isolated_db: None) -> None:
    now = datetime(2026, 8, 17, 9, 0, tzinfo=TZ)
    item_id = _seed_item(
        "2026-08-14", "tasks_due", "google_tasks", "t1", "Renew cert",
        created_at=now.isoformat(), due_date="2026-08-15",
    )

    result = nudges.compute_nudges("2026-08-17", now=now)

    assert len(result) == 1
    assert result[0]["id"] == "overdue_tasks"
    assert "Renew cert" in result[0]["message"]
    assert result[0]["targets"] == [{"id": item_id}]


def test_overdue_task_nudge_does_not_fire_for_a_future_due_date(isolated_db: None) -> None:
    now = datetime(2026, 8, 17, 9, 0, tzinfo=TZ)
    _seed_item(
        "2026-08-17", "tasks_due", "google_tasks", "t2", "Not due yet",
        created_at=now.isoformat(), due_date="2026-08-20",
    )

    assert nudges.compute_nudges("2026-08-17", now=now) == []


def _set_last_pulled(source_name: str, last_pulled_at: str | None) -> None:
    with db.session() as conn:
        conn.execute(
            "UPDATE source_config SET last_pulled_at = ? WHERE source_name = ?",
            (last_pulled_at, source_name),
        )


def test_no_pull_today_nudge_fires_in_the_afternoon_with_no_pulls(isolated_db: None) -> None:
    now = datetime(2026, 8, 17, 14, 0, tzinfo=TZ)
    queries.seed_source_config(["gmail"])
    _set_last_pulled("gmail", None)

    result = nudges.compute_nudges("2026-08-17", now=now)

    assert any(n["id"] == "no_pull_today" for n in result)
    assert "targets" not in next(n for n in result if n["id"] == "no_pull_today")


def test_no_pull_today_nudge_does_not_fire_in_the_morning(isolated_db: None) -> None:
    now = datetime(2026, 8, 17, 9, 0, tzinfo=TZ)
    queries.seed_source_config(["gmail"])
    _set_last_pulled("gmail", None)

    result = nudges.compute_nudges("2026-08-17", now=now)

    assert not any(n["id"] == "no_pull_today" for n in result)


def test_no_pull_today_nudge_does_not_fire_when_a_source_pulled_today(isolated_db: None) -> None:
    now = datetime(2026, 8, 17, 14, 0, tzinfo=TZ)
    queries.seed_source_config(["gmail"])
    _set_last_pulled("gmail", now.isoformat())

    result = nudges.compute_nudges("2026-08-17", now=now)

    assert not any(n["id"] == "no_pull_today" for n in result)


def test_no_pull_today_nudge_ignores_disabled_sources(isolated_db: None) -> None:
    now = datetime(2026, 8, 17, 14, 0, tzinfo=TZ)
    queries.seed_source_config(["gmail"])
    _set_last_pulled("gmail", None)
    queries.update_source_config("gmail", enabled=False)

    result = nudges.compute_nudges("2026-08-17", now=now)

    assert not any(n["id"] == "no_pull_today" for n in result)
