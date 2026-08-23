from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, queries
from command_center.assistant import ingest
from command_center.config import TZ
from command_center.fitness import aggregations, charts
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


# --- hidden page --------------------------------------------------------------


def test_fitness_is_not_linked_from_the_nav_or_command_palette(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert "/fitness" not in response.text


def test_fitness_dashboard_reachable_by_direct_url(client: TestClient) -> None:
    response = client.get("/fitness")
    assert response.status_code == 200
    assert "Fitness" in response.text


def test_fitness_settings_reachable_by_direct_url(client: TestClient) -> None:
    response = client.get("/fitness/settings")
    assert response.status_code == 200


def test_footer_disclaimer_present_on_both_pages(client: TestClient) -> None:
    for path in ("/fitness", "/fitness/settings"):
        response = client.get(path)
        assert "Personal tracking tool, not medical or nutritional advice." in response.text


# --- settings seeding + CRUD --------------------------------------------------


def test_fitness_settings_seeded_with_starting_values(client: TestClient) -> None:
    settings = queries.get_fitness_settings()
    assert settings["goal_weight_low"] == 60.0
    assert settings["goal_weight_high"] == 65.0
    assert settings["height_cm"] == 178.0
    assert settings["age"] == 24
    assert settings["current_weight"] == 53.7
    assert settings["protein_target_g_per_day"] == 100.0
    assert settings["resistance_sessions_per_week_target"] == 3
    assert settings["running_sessions_per_week_cap"] == 2
    assert settings["running_weekly_distance_cap_km"] is None


def test_seeding_is_idempotent_and_never_clobbers_edits(client: TestClient) -> None:
    queries.update_fitness_settings(
        goal_weight_low=61,
        goal_weight_high=66,
        height_cm=178,
        age=25,
        current_weight=55,
        protein_target_g_per_day=110,
        resistance_sessions_per_week_target=4,
        running_sessions_per_week_cap=2,
        running_weekly_distance_cap_km=20,
    )
    queries.seed_fitness_settings()
    assert queries.get_fitness_settings()["current_weight"] == 55


def test_starter_surplus_addons_are_seeded(client: TestClient) -> None:
    names = {a["name"] for a in queries.list_surplus_addons()}
    assert names == {"Olive oil", "Peanut butter"}


def test_update_fitness_settings_route_persists(client: TestClient) -> None:
    response = client.patch(
        "/fitness/settings",
        json={
            "goal_weight_low": 61,
            "goal_weight_high": 66,
            "height_cm": 178,
            "age": 25,
            "current_weight": 54,
            "protein_target_g_per_day": 120,
            "resistance_sessions_per_week_target": 4,
            "running_sessions_per_week_cap": 2,
            "running_weekly_distance_cap_km": 15,
        },
    )
    assert response.status_code == 200
    settings = queries.get_fitness_settings()
    assert settings["goal_weight_low"] == 61
    assert settings["protein_target_g_per_day"] == 120
    assert settings["running_weekly_distance_cap_km"] == 15


def test_update_fitness_settings_rejects_inverted_goal_band(client: TestClient) -> None:
    response = client.patch(
        "/fitness/settings",
        json={
            "goal_weight_low": 70,
            "goal_weight_high": 60,
            "height_cm": 178,
            "age": 25,
            "current_weight": 54,
            "protein_target_g_per_day": 100,
            "resistance_sessions_per_week_target": 3,
            "running_sessions_per_week_cap": 2,
        },
    )
    assert response.status_code == 400


def test_running_cap_can_be_cleared_back_to_null(client: TestClient) -> None:
    queries.update_fitness_settings(
        goal_weight_low=60, goal_weight_high=65, height_cm=178, age=24, current_weight=54,
        protein_target_g_per_day=100, resistance_sessions_per_week_target=3,
        running_sessions_per_week_cap=2, running_weekly_distance_cap_km=20,
    )
    response = client.patch(
        "/fitness/settings",
        json={
            "goal_weight_low": 60, "goal_weight_high": 65, "height_cm": 178, "age": 24,
            "current_weight": 54, "protein_target_g_per_day": 100,
            "resistance_sessions_per_week_target": 3, "running_sessions_per_week_cap": 2,
            "running_weekly_distance_cap_km": None,
        },
    )
    assert response.status_code == 200
    assert queries.get_fitness_settings()["running_weekly_distance_cap_km"] is None


def test_add_and_remove_surplus_addon_via_routes(client: TestClient) -> None:
    add = client.post("/fitness/settings/addons", json={"name": "Whole milk"})
    assert add.status_code == 200
    addon_id = add.json()["id"]
    assert "Whole milk" in {a["name"] for a in queries.list_surplus_addons()}

    delete = client.delete(f"/fitness/settings/addons/{addon_id}")
    assert delete.status_code == 200
    assert "Whole milk" not in {a["name"] for a in queries.list_surplus_addons()}


def test_duplicate_surplus_addon_name_is_rejected(client: TestClient) -> None:
    response = client.post("/fitness/settings/addons", json={"name": "Olive oil"})
    assert response.status_code == 400


def test_removing_an_addon_does_not_alter_a_past_days_logged_history(client: TestClient) -> None:
    add = client.post("/fitness/settings/addons", json={"name": "Whole milk"})
    addon_id = add.json()["id"]
    queries.upsert_daily_log("2026-08-10", None, None, ["Whole milk"], "")

    client.delete(f"/fitness/settings/addons/{addon_id}")

    log = queries.get_daily_log("2026-08-10")
    assert log["addons_checked"] == ["Whole milk"]


# --- daily log ------------------------------------------------------------


def test_daily_log_upsert_updates_same_day_instead_of_duplicating(client: TestClient) -> None:
    queries.upsert_daily_log("2026-08-10", 54.0, 90, ["Olive oil"], "felt good")
    queries.upsert_daily_log("2026-08-10", 54.2, 105, ["Peanut butter"], "updated")

    logs = queries.list_daily_logs("2026-08-01", "2026-09-01")
    assert len(logs) == 1
    assert logs[0]["bodyweight_kg"] == 54.2
    assert logs[0]["protein_g"] == 105
    assert logs[0]["addons_checked"] == ["Peanut butter"]
    assert logs[0]["note"] == "updated"


def test_list_daily_logs_respects_half_open_bounds(client: TestClient) -> None:
    queries.upsert_daily_log("2026-07-31", 54, None, [], "")
    queries.upsert_daily_log("2026-08-01", 54, None, [], "")
    queries.upsert_daily_log("2026-08-31", 54, None, [], "")
    queries.upsert_daily_log("2026-09-01", 54, None, [], "")

    dates = {log["log_date"] for log in queries.list_daily_logs("2026-08-01", "2026-09-01")}
    assert dates == {"2026-08-01", "2026-08-31"}


def test_save_daily_log_route_persists_and_syncs_current_weight_only_for_today(
    client: TestClient,
) -> None:
    today_iso = datetime.now(TZ).date().isoformat()

    response = client.post(
        "/fitness/daily-log",
        json={"log_date": today_iso, "bodyweight_kg": 55.5, "protein_g": 95, "addons_checked": ["Olive oil"], "note": "x"},
    )
    assert response.status_code == 200
    assert queries.get_fitness_settings()["current_weight"] == 55.5

    # A backfilled past date must never overwrite the "current" figure.
    response = client.post(
        "/fitness/daily-log",
        json={"log_date": "2020-01-01", "bodyweight_kg": 40.0, "protein_g": None, "addons_checked": [], "note": ""},
    )
    assert response.status_code == 200
    assert queries.get_fitness_settings()["current_weight"] == 55.5


def test_list_bodyweight_log_omits_days_with_no_weight_and_is_ascending(client: TestClient) -> None:
    queries.upsert_daily_log("2026-08-05", None, 100, [], "")
    queries.upsert_daily_log("2026-08-01", 54, 100, [], "")
    queries.upsert_daily_log("2026-08-10", 55, 100, [], "")

    points = queries.list_bodyweight_log()
    assert [p["log_date"] for p in points] == ["2026-08-01", "2026-08-10"]


# --- training sessions ------------------------------------------------------


def test_create_training_session_computes_volume_from_exercises(client: TestClient) -> None:
    session_id = queries.create_training_session(
        "2026-08-10",
        [
            {"name": "Squat", "sets": 3, "reps": 5, "weight_kg": 60},
            {"name": "Bench", "sets": 3, "reps": 8, "weight_kg": 40},
        ],
    )
    sessions = queries.list_training_sessions("2026-08-01", "2026-09-01")
    assert len(sessions) == 1
    assert sessions[0]["id"] == session_id
    assert sessions[0]["volume"] == 3 * 5 * 60 + 3 * 8 * 40
    assert [e["name"] for e in sessions[0]["exercises"]] == ["Squat", "Bench"]


def test_delete_training_session_removes_its_exercises_too(client: TestClient) -> None:
    session_id = queries.create_training_session(
        "2026-08-10", [{"name": "Squat", "sets": 3, "reps": 5, "weight_kg": 60}]
    )
    assert queries.delete_training_session(session_id) is True
    assert queries.list_training_sessions("2026-08-01", "2026-09-01") == []
    assert queries.delete_training_session(session_id) is False


def test_save_training_session_route_rejects_no_named_exercises(client: TestClient) -> None:
    response = client.post(
        "/fitness/training-sessions",
        json={"session_date": "2026-08-10", "exercises": [{"name": "  ", "sets": 3, "reps": 5, "weight_kg": 60}]},
    )
    assert response.status_code == 400


def test_save_training_session_route_persists(client: TestClient) -> None:
    response = client.post(
        "/fitness/training-sessions",
        json={
            "session_date": "2026-08-10",
            "exercises": [{"name": "Deadlift", "sets": 1, "reps": 5, "weight_kg": 80}],
        },
    )
    assert response.status_code == 200
    session_id = response.json()["id"]

    delete = client.delete(f"/fitness/training-sessions/{session_id}")
    assert delete.status_code == 200
    assert client.delete(f"/fitness/training-sessions/{session_id}").status_code == 404


# --- running log ------------------------------------------------------------


def test_create_and_list_running_logs(client: TestClient) -> None:
    queries.create_running_log("2026-08-10", 5.0, "easy")
    logs = queries.list_running_logs("2026-08-01", "2026-09-01")
    assert len(logs) == 1
    assert logs[0]["distance_km"] == 5.0
    assert logs[0]["intensity"] == "easy"


def test_save_running_log_route_rejects_unknown_intensity(client: TestClient) -> None:
    response = client.post(
        "/fitness/running-log", json={"run_date": "2026-08-10", "distance_km": 5, "intensity": "sprint"}
    )
    assert response.status_code == 400


def test_save_running_log_route_rejects_non_positive_distance(client: TestClient) -> None:
    response = client.post(
        "/fitness/running-log", json={"run_date": "2026-08-10", "distance_km": 0, "intensity": "easy"}
    )
    assert response.status_code == 400


def test_save_running_log_route_persists_and_deletes(client: TestClient) -> None:
    response = client.post(
        "/fitness/running-log", json={"run_date": "2026-08-10", "distance_km": 8.2, "intensity": "moderate"}
    )
    assert response.status_code == 200
    run_id = response.json()["id"]

    delete = client.delete(f"/fitness/running-log/{run_id}")
    assert delete.status_code == 200
    assert client.delete(f"/fitness/running-log/{run_id}").status_code == 404


# --- aggregations / status chips --------------------------------------------


def test_protein_status_counts_met_days_over_trailing_week(client: TestClient) -> None:
    end = date(2026, 8, 10)
    for i in range(7):
        d = (end - timedelta(days=i)).isoformat()
        queries.upsert_daily_log(d, None, 100 if i % 2 == 0 else 50, [], "")

    status = aggregations.protein_status(end, target_g=90)
    assert status["met_days"] == 4  # i = 0, 2, 4, 6
    assert status["status"] == "green"  # most recent day (i=0) met


def test_protein_status_is_amber_only_after_two_consecutive_recent_misses(client: TestClient) -> None:
    end = date(2026, 8, 10)
    queries.upsert_daily_log(end.isoformat(), None, 50, [], "")
    queries.upsert_daily_log((end - timedelta(days=1)).isoformat(), None, 50, [], "")

    status = aggregations.protein_status(end, target_g=90)
    assert status["status"] == "amber"


def test_protein_status_ignores_an_unlogged_day_as_not_met_but_not_flagged_alone(client: TestClient) -> None:
    end = date(2026, 8, 10)
    queries.upsert_daily_log(end.isoformat(), None, 100, [], "")
    # yesterday: no log at all

    status = aggregations.protein_status(end, target_g=90)
    assert status["status"] == "green"  # only 1 of the last 2 days missed, not both


def test_surplus_addon_status_counts_days_with_at_least_one_checked(client: TestClient) -> None:
    end = date(2026, 8, 10)
    queries.upsert_daily_log(end.isoformat(), None, None, ["Olive oil"], "")
    queries.upsert_daily_log(
        (end - timedelta(days=1)).isoformat(), None, None, [], ""
    )

    status = aggregations.surplus_addon_status(end)
    assert status["logged_days"] == 1
    assert status["status"] == "green"


def test_training_volume_status_flat_percent_and_amber_on_drop(client: TestClient) -> None:
    end = date(2026, 8, 14)
    # previous 7-day window: 2026-08-00..06 -> use 08-07..08-13 is "this window";
    # previous window is 08-00.. i.e. 07-31..08-06.
    queries.create_training_session("2026-08-01", [{"name": "Squat", "sets": 1, "reps": 1, "weight_kg": 100}])
    queries.create_training_session("2026-08-10", [{"name": "Squat", "sets": 1, "reps": 1, "weight_kg": 50}])

    status = aggregations.training_volume_status(end)
    assert status["last_week"] == 100
    assert status["this_week"] == 50
    assert status["pct_change"] == -50
    assert status["status"] == "amber"


def test_training_volume_status_has_no_pct_when_previous_week_was_empty(client: TestClient) -> None:
    end = date(2026, 8, 14)
    queries.create_training_session("2026-08-10", [{"name": "Squat", "sets": 1, "reps": 1, "weight_kg": 50}])

    status = aggregations.training_volume_status(end)
    assert status["last_week"] == 0
    assert status["pct_change"] is None
    assert status["status"] == "green"  # never amber when there's nothing to have dropped from


def test_running_status_amber_only_when_a_cap_is_set_and_exceeded(client: TestClient) -> None:
    end = date(2026, 8, 10)
    queries.create_running_log("2026-08-09", 20, "easy")

    assert aggregations.running_status(end, cap_km=15)["status"] == "amber"
    assert aggregations.running_status(end, cap_km=25)["status"] == "green"
    assert aggregations.running_status(end, cap_km=None)["status"] == "green"


def test_headline_stats_gap_to_goal_and_week_counts(client: TestClient) -> None:
    end = date(2026, 8, 10)
    queries.create_training_session("2026-08-09", [{"name": "Squat", "sets": 1, "reps": 1, "weight_kg": 10}])
    queries.create_running_log("2026-08-09", 5, "easy")
    settings = {
        "current_weight": 53.7,
        "goal_weight_low": 60.0,
        "resistance_sessions_per_week_target": 3,
        "running_sessions_per_week_cap": 2,
    }

    stats = aggregations.headline_stats(end, settings)
    assert stats["gap_to_goal_low"] == pytest.approx(6.3)
    assert stats["sessions_this_week"] == 1
    assert stats["runs_this_week"] == 1


def test_weekly_series_are_oldest_first_and_length_matches_num_weeks(client: TestClient) -> None:
    end = date(2026, 8, 14)
    series = aggregations.weekly_volume_series(end, num_weeks=4)
    assert len(series) == 4
    assert all("bucket" in b and "count" in b for b in series)


# --- charts ------------------------------------------------------------------


def test_weight_trend_svg_handles_zero_points(client: TestClient) -> None:
    svg = charts.render_weight_trend_svg([], goal_low=60, goal_high=65)
    assert svg.startswith("<svg")
    assert "No weight logged yet" in svg


def test_weight_trend_svg_draws_a_point_per_entry(client: TestClient) -> None:
    points = [{"log_date": "2026-08-01", "bodyweight_kg": 54.0}, {"log_date": "2026-08-10", "bodyweight_kg": 55.0}]
    svg = charts.render_weight_trend_svg(points, goal_low=60, goal_high=65)
    assert svg.count("<circle") == 2


def test_bars_svg_draws_a_cap_line_only_when_given(client: TestClient) -> None:
    buckets = [{"bucket": "Aug 01", "count": 10}, {"bucket": "Aug 08", "count": 20}]
    with_cap = charts.render_bars_svg(buckets, cap_value=15)
    without_cap = charts.render_bars_svg(buckets, cap_value=None)
    assert "stroke-dasharray" in with_cap
    assert "stroke-dasharray" not in without_cap
    assert with_cap.count("<rect") >= 2


# --- monthly CSV exports -------------------------------------------------------


def test_dashboard_shows_the_three_download_links(client: TestClient) -> None:
    response = client.get("/fitness")
    assert 'href="/fitness/export/daily-log.csv"' in response.text
    assert 'href="/fitness/export/training.csv"' in response.text
    assert 'href="/fitness/export/running.csv"' in response.text


def test_export_daily_log_csv_includes_this_months_entry(client: TestClient) -> None:
    today_iso = datetime.now(TZ).date().isoformat()
    queries.upsert_daily_log(today_iso, 54.2, 105, ["Olive oil"], "note here")

    response = client.get("/fitness/export/daily-log.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == f"attachment; filename=fitness-daily-log-{today_iso[:7]}.csv"
    assert response.text.startswith("log_date,")
    assert "Olive oil" in response.text
    assert "note here" in response.text


def test_export_daily_log_csv_excludes_a_different_month(client: TestClient) -> None:
    queries.upsert_daily_log("2020-01-15", 50, 90, [], "old entry")
    response = client.get("/fitness/export/daily-log.csv")
    assert "old entry" not in response.text


def test_export_training_csv_is_one_row_per_exercise(client: TestClient) -> None:
    today_iso = datetime.now(TZ).date().isoformat()
    queries.create_training_session(
        today_iso,
        [
            {"name": "Squat", "sets": 3, "reps": 5, "weight_kg": 60},
            {"name": "Bench", "sets": 3, "reps": 8, "weight_kg": 40},
        ],
    )

    response = client.get("/fitness/export/training.csv")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == f"attachment; filename=fitness-training-{today_iso[:7]}.csv"
    lines = [line for line in response.text.strip().splitlines() if line]
    assert len(lines) == 3  # header + 2 exercise rows
    assert "Squat" in response.text and "Bench" in response.text
    assert "900" in response.text  # 3 * 5 * 60 volume_kg


def test_export_running_csv(client: TestClient) -> None:
    today_iso = datetime.now(TZ).date().isoformat()
    queries.create_running_log(today_iso, 6.5, "easy")

    response = client.get("/fitness/export/running.csv")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == f"attachment; filename=fitness-running-{today_iso[:7]}.csv"
    assert "6.5" in response.text
    assert "easy" in response.text


def test_all_three_exports_are_empty_strings_not_errors_with_no_data(client: TestClient) -> None:
    for path in ("/fitness/export/daily-log.csv", "/fitness/export/training.csv", "/fitness/export/running.csv"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.text == ""
