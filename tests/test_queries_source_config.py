from pathlib import Path

import pytest

from command_center import db, queries


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_seed_source_config_sets_all_rows_enabled_with_last_pulled_at(isolated_db: None) -> None:
    queries.seed_source_config(["gmail", "calendar", "tasks", "medium"])
    configs = {c["source_name"]: c for c in queries.list_source_configs()}

    assert set(configs) == {"gmail", "calendar", "tasks", "medium"}
    for cfg in configs.values():
        assert cfg["enabled"] == 1
        assert cfg["interval_minutes"] == 60
        assert cfg["last_pulled_at"] is not None


def test_seed_source_config_overrides_set_per_source_intervals(isolated_db: None) -> None:
    queries.seed_source_config(
        ["gmail", "calendar", "medium"],
        default_interval_minutes=60,
        overrides={"gmail": 240, "medium": 1440},
    )
    configs = {c["source_name"]: c["interval_minutes"] for c in queries.list_source_configs()}

    assert configs["gmail"] == 240
    assert configs["medium"] == 1440
    assert configs["calendar"] == 60  # not in overrides — falls back to the shared default


def test_seed_source_config_does_not_overwrite_existing_rows(isolated_db: None) -> None:
    queries.seed_source_config(["gmail"])
    queries.update_source_config("gmail", enabled=False, interval_minutes=15)

    queries.seed_source_config(["gmail"])  # re-seed, e.g. on next app startup

    cfg = queries.list_source_configs()[0]
    assert cfg["enabled"] == 0
    assert cfg["interval_minutes"] == 15


def test_update_source_config_partial_update_leaves_other_field_untouched(
    isolated_db: None,
) -> None:
    queries.seed_source_config(["gmail"])

    queries.update_source_config("gmail", interval_minutes=30)
    cfg = queries.list_source_configs()[0]
    assert cfg["interval_minutes"] == 30
    assert cfg["enabled"] == 1  # untouched

    queries.update_source_config("gmail", enabled=False)
    cfg = queries.list_source_configs()[0]
    assert cfg["enabled"] == 0
    assert cfg["interval_minutes"] == 30  # untouched


def test_touch_source_last_pulled_updates_timestamp(isolated_db: None) -> None:
    queries.seed_source_config(["gmail"])
    original = queries.list_source_configs()[0]["last_pulled_at"]

    queries.touch_source_last_pulled("gmail")

    updated = queries.list_source_configs()[0]["last_pulled_at"]
    assert updated >= original


def test_save_triage_results_marks_the_brief_as_not_fixture(isolated_db: None) -> None:
    queries.save_triage_results(
        brief_date="2026-08-14",
        triaged_items=[],
        calendar_events=[],
        degraded_sources=[],
        sources_attempted=["gmail"],
        force=False,
    )
    with db.session() as conn:
        row = conn.execute(
            "SELECT is_fixture FROM briefs WHERE brief_date = ?", ("2026-08-14",)
        ).fetchone()
    assert row["is_fixture"] == 0


def test_save_triage_results_clears_a_pre_existing_fixture_flag(isolated_db: None) -> None:
    # The "connect Google later" transition: a day that started as fixture
    # demo data (fixtures.seed()) must stop being flagged as such the
    # moment a real pipeline run actually saves results for that date.
    with db.session() as conn:
        conn.execute(
            "INSERT INTO briefs (brief_date, generated_at, degraded_lanes, is_fixture) "
            "VALUES (?, ?, '[]', 1)",
            ("2026-08-14", "2026-08-14T09:00:00+00:00"),
        )

    queries.save_triage_results(
        brief_date="2026-08-14",
        triaged_items=[],
        calendar_events=[],
        degraded_sources=[],
        sources_attempted=["gmail"],
        force=False,
    )

    with db.session() as conn:
        row = conn.execute(
            "SELECT is_fixture FROM briefs WHERE brief_date = ?", ("2026-08-14",)
        ).fetchone()
    assert row["is_fixture"] == 0


def test_save_triage_results_only_touches_calendar_when_attempted(isolated_db: None) -> None:
    queries.save_triage_results(
        brief_date="2026-08-14",
        triaged_items=[],
        calendar_events=[
            {
                "title": "Standup",
                "start_time": "2026-08-14T09:30:00+03:00",
                "end_time": "2026-08-14T10:00:00+03:00",
                "attendees": "[]",
                "link": None,
            }
        ],
        degraded_sources=[],
        sources_attempted=["gmail", "calendar", "tasks", "medium"],
        force=False,
    )

    # A gmail-only pull must not wipe today's calendar timeline strip.
    queries.save_triage_results(
        brief_date="2026-08-14",
        triaged_items=[],
        calendar_events=[],
        degraded_sources=[],
        sources_attempted=["gmail"],
        force=False,
    )

    brief = queries.get_brief("2026-08-14")
    assert len(brief["events"]) == 1
    assert brief["events"][0]["title"] == "Standup"


def test_save_triage_results_merges_degraded_lanes_across_calls(isolated_db: None) -> None:
    queries.save_triage_results(
        brief_date="2026-08-14",
        triaged_items=[],
        calendar_events=[],
        degraded_sources=["tasks", "medium"],
        sources_attempted=["gmail", "calendar", "tasks", "medium"],
        force=False,
    )

    # Only gmail is attempted this round, and it succeeds — tasks/medium's
    # prior degraded status (not re-attempted) must survive untouched.
    queries.save_triage_results(
        brief_date="2026-08-14",
        triaged_items=[],
        calendar_events=[],
        degraded_sources=[],
        sources_attempted=["gmail"],
        force=False,
    )

    brief = queries.get_brief("2026-08-14")
    assert brief["degraded_lanes"] == ["tasks", "medium"]


def test_save_triage_results_drops_source_from_degraded_once_it_succeeds(
    isolated_db: None,
) -> None:
    queries.save_triage_results(
        brief_date="2026-08-14",
        triaged_items=[],
        calendar_events=[],
        degraded_sources=["tasks"],
        sources_attempted=["gmail", "calendar", "tasks", "medium"],
        force=False,
    )

    # tasks is retried this round and succeeds this time.
    queries.save_triage_results(
        brief_date="2026-08-14",
        triaged_items=[],
        calendar_events=[],
        degraded_sources=[],
        sources_attempted=["tasks"],
        force=False,
    )

    brief = queries.get_brief("2026-08-14")
    assert brief["degraded_lanes"] == []
