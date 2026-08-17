from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, notify, pipeline, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.config import TZ
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    # Fixture mode: never let the app's startup lifespan trigger a real
    # Gmail/Calendar/Tasks call via this machine's actual Google token.
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def test_settings_page_lists_all_seeded_sources(client: TestClient) -> None:
    response = client.get("/settings")
    assert response.status_code == 200
    for name in ("gmail", "calendar", "tasks", "medium"):
        assert name in response.text


def test_settings_source_toggles_revert_on_a_failed_save(client: TestClient) -> None:
    # Regression guard for the silent-failure-UI fix: a failed PATCH must
    # not leave the checkbox/select showing a value the server never
    # actually accepted.
    response = client.get("/settings")
    assert response.status_code == 200
    assert "this.enabled = !value" in response.text
    assert "this.interval = this.savedInterval" in response.text


def test_settings_page_includes_tooltip_toggle(client: TestClient) -> None:
    response = client.get("/settings")
    assert response.status_code == 200
    assert "Show icon labels on hover" in response.text


# --- system health --------------------------------------------------------


def test_health_endpoint_returns_expected_keys(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["google_connected"] is False
    assert "triage_provider" in data
    assert data["degraded_today"] == []
    assert len(data["sources"]) == 4
    assert "assistant_index" in data


def test_health_endpoint_reports_degraded_when_a_source_failed_today(
    client: TestClient,
) -> None:
    from datetime import datetime

    from command_center.config import TZ

    today = datetime.now(TZ).date().isoformat()
    queries.save_triage_results(
        brief_date=today,
        triaged_items=[],
        calendar_events=[],
        degraded_sources=["gmail"],
        sources_attempted=["gmail"],
        force=False,
    )

    response = client.get("/health")

    data = response.json()
    assert data["status"] == "degraded"
    assert data["degraded_today"] == ["gmail"]


def test_settings_page_shows_system_status_card(client: TestClient) -> None:
    response = client.get("/settings")
    assert response.status_code == 200
    assert "System status" in response.text
    assert "Not connected" in response.text  # has_valid_credentials is False in this fixture


def test_settings_page_links_to_activity(client: TestClient) -> None:
    response = client.get("/settings")
    assert response.status_code == 200
    assert 'href="/settings/activity"' in response.text


def test_activity_page_empty_state(client: TestClient) -> None:
    response = client.get("/settings/activity")
    assert response.status_code == 200
    assert "No tool calls yet." in response.text
    assert "No item activity yet." in response.text


def test_activity_page_shows_tool_call_and_status(client: TestClient) -> None:
    queries.log_tool_call("create_task", {"title": "Renew passport"}, "confirmed")

    response = client.get("/settings/activity")

    assert "create_task" in response.text
    assert "Renew passport" in response.text
    assert "confirmed" in response.text


def test_activity_page_shows_item_history_event(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-08-17", "urgent", "Fix the thing")
    queries.set_item_status(item_id, "done")

    response = client.get("/settings/activity")

    assert "Fix the thing" in response.text
    assert "completed" in response.text


def test_settings_page_links_to_triage_rules(client: TestClient) -> None:
    response = client.get("/settings")
    assert 'href="/settings/triage-rules"' in response.text


def test_triage_rules_page_empty_state(client: TestClient) -> None:
    response = client.get("/settings/triage-rules")
    assert response.status_code == 200
    assert "No rules yet." in response.text


def test_create_triage_rule_route(client: TestClient) -> None:
    response = client.post(
        "/settings/triage-rules",
        json={"field": "title", "match_value": "server down", "lane": "urgent"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert queries.list_triage_rules()[0]["match_value"] == "server down"


def test_create_triage_rule_rejects_unknown_field(client: TestClient) -> None:
    response = client.post(
        "/settings/triage-rules", json={"field": "body", "match_value": "x", "lane": "urgent"}
    )
    assert response.status_code == 400


def test_create_triage_rule_rejects_blank_match_value(client: TestClient) -> None:
    response = client.post(
        "/settings/triage-rules", json={"field": "title", "match_value": "  ", "lane": "urgent"}
    )
    assert response.status_code == 400


def test_create_triage_rule_rejects_unknown_lane(client: TestClient) -> None:
    response = client.post(
        "/settings/triage-rules", json={"field": "title", "match_value": "x", "lane": "not_a_lane"}
    )
    assert response.status_code == 400


def test_toggle_triage_rule_route(client: TestClient) -> None:
    rule_id = queries.create_triage_rule("title", "x", "urgent")

    response = client.patch(f"/settings/triage-rules/{rule_id}", json={"enabled": False})

    assert response.status_code == 200
    assert queries.list_triage_rules()[0]["enabled"] == 0


def test_toggle_triage_rule_route_404s_for_unknown_id(client: TestClient) -> None:
    response = client.patch("/settings/triage-rules/99999", json={"enabled": False})
    assert response.status_code == 404


def test_delete_triage_rule_route(client: TestClient) -> None:
    rule_id = queries.create_triage_rule("title", "x", "urgent")

    response = client.delete(f"/settings/triage-rules/{rule_id}")

    assert response.status_code == 200
    assert queries.list_triage_rules() == []


def test_delete_triage_rule_route_404s_for_unknown_id(client: TestClient) -> None:
    response = client.delete("/settings/triage-rules/99999")
    assert response.status_code == 404


def test_settings_page_links_to_lane_labels(client: TestClient) -> None:
    response = client.get("/settings")
    assert 'href="/settings/lane-labels"' in response.text


def test_lane_labels_page_shows_current_labels(client: TestClient) -> None:
    response = client.get("/settings/lane-labels")
    assert response.status_code == 200
    assert "urgent" in response.text


def test_update_lane_label_route(client: TestClient) -> None:
    response = client.post("/settings/lane-labels/urgent", json={"label": "Fires"})
    assert response.status_code == 200
    assert response.json()["label"] == "Fires"
    assert queries.get_lane_labels()["urgent"] == "Fires"


def test_update_lane_label_route_rejects_unknown_lane(client: TestClient) -> None:
    response = client.post("/settings/lane-labels/not_a_lane", json={"label": "X"})
    assert response.status_code == 404


def test_update_lane_label_route_blank_resets_to_default(client: TestClient) -> None:
    client.post("/settings/lane-labels/urgent", json={"label": "Fires"})
    response = client.post("/settings/lane-labels/urgent", json={"label": ""})

    assert response.status_code == 200
    from command_center.config import LANE_LABELS

    assert response.json()["label"] == LANE_LABELS["urgent"]


def test_a_renamed_lane_label_shows_on_the_brief(client: TestClient) -> None:
    client.post("/settings/lane-labels/urgent", json={"label": "Fires"})
    response = client.get("/brief")
    assert "Fires" in response.text


def test_triage_rules_page_shows_no_corrections_empty_state(client: TestClient) -> None:
    response = client.get("/settings/triage-rules")
    assert "No corrections yet" in response.text


def test_triage_rules_page_shows_a_recent_correction(client: TestClient) -> None:
    today = datetime.now(TZ).date().isoformat()
    with db.session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO briefs (brief_date, generated_at, degraded_lanes) VALUES (?, ?, '[]')",
            (today, datetime.now(TZ).isoformat()),
        )
        cursor = conn.execute(
            "INSERT INTO items (brief_date, lane, source, source_id, title, why_it_matters, "
            "suggested_next_step, priority, deep_link, status, created_at) "
            "VALUES (?, 'reading', 'gmail', 'g1', 'Newsletter', '', '', 2, '', 'pending', ?)",
            (today, datetime.now(TZ).isoformat()),
        )
        item_id = cursor.lastrowid
    queries.update_item_lane(item_id, "urgent")

    response = client.get("/settings/triage-rules")

    assert "Newsletter" in response.text
    assert "Recent corrections" in response.text


def test_triage_rules_page_shows_a_suggested_rule_pattern(client: TestClient) -> None:
    today = datetime.now(TZ).date().isoformat()
    item_ids = []
    with db.session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO briefs (brief_date, generated_at, degraded_lanes) VALUES (?, ?, '[]')",
            (today, datetime.now(TZ).isoformat()),
        )
        for source_id in ("g1", "g2"):
            cursor = conn.execute(
                "INSERT INTO items (brief_date, lane, source, source_id, title, why_it_matters, "
                "suggested_next_step, priority, deep_link, status, created_at) "
                "VALUES (?, 'reading', 'gmail', ?, 'Item', '', '', 2, '', 'pending', ?)",
                (today, source_id, datetime.now(TZ).isoformat()),
            )
            item_ids.append(cursor.lastrowid)

    for item_id in item_ids:
        queries.update_item_lane(item_id, "urgent")

    response = client.get("/settings/triage-rules")

    assert "Suggested rules" in response.text
    assert "Moved 2" in response.text


def test_settings_page_shows_notifications_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "NTFY_TOPIC", "")
    response = client.get("/settings")
    assert "Not configured" in response.text


def test_settings_page_shows_notifications_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "NTFY_TOPIC", "my-topic")
    response = client.get("/settings")
    assert "ntfy configured" in response.text


def test_send_test_notification_route_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "send", lambda *a, **k: True)
    response = client.post("/settings/notifications/test")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_send_test_notification_route_failure_surfaces_an_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "send", lambda *a, **k: False)
    monkeypatch.setattr(notify, "NTFY_TOPIC", "my-topic")
    response = client.post("/settings/notifications/test")
    assert response.status_code == 502
    assert "ntfy" in response.json()["detail"].lower()


def test_send_test_notification_route_failure_when_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "send", lambda *a, **k: False)
    monkeypatch.setattr(notify, "NTFY_TOPIC", "")
    response = client.post("/settings/notifications/test")
    assert response.status_code == 502
    assert "NTFY_TOPIC" in response.json()["detail"]


def test_notify_new_nudges_sends_once_and_dedups_on_repeated_ticks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import app as app_module
    from datetime import timedelta

    monkeypatch.setattr(app_module.notify, "NTFY_TOPIC", "my-topic")
    calls = []
    monkeypatch.setattr(app_module.notify, "send", lambda *a, **k: calls.append(a) or True)

    today = datetime.now(TZ).date().isoformat()
    old = (datetime.now(TZ) - timedelta(days=3)).isoformat()
    with db.session() as conn:
        conn.execute(
            "INSERT INTO items (brief_date, lane, source, source_id, title, why_it_matters, "
            "suggested_next_step, priority, deep_link, status, created_at) "
            "VALUES (?, 'urgent', 'gmail', 'stale-notify', 'Stale item', '', '', 1, '', "
            "'pending', ?)",
            (today, old),
        )

    app_module._notify_new_nudges()
    app_module._notify_new_nudges()  # a second tick, same day — must not double-send

    assert len(calls) == 1


def test_notify_new_nudges_is_a_no_op_when_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import app as app_module

    monkeypatch.setattr(app_module.notify, "NTFY_TOPIC", "")
    calls = []
    monkeypatch.setattr(app_module.notify, "send", lambda *a, **k: calls.append(a) or True)

    app_module._notify_new_nudges()

    assert calls == []


def test_settings_page_has_export_links(client: TestClient) -> None:
    response = client.get("/settings")
    assert 'href="/settings/export.json"' in response.text
    assert 'href="/settings/export/items.csv"' in response.text
    assert 'href="/settings/export/finances.csv"' in response.text


def test_export_json_includes_everything(client: TestClient) -> None:
    today = datetime.now(TZ).date().isoformat()
    queries.create_manual_item(today, "urgent", "Renew passport")
    queries.create_finance_entry(42.5, "Food", "spend", today)

    response = client.get("/settings/export.json")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == "attachment; filename=command-center-export.json"
    data = response.json()
    assert "exported_at" in data
    assert any(item["title"] == "Renew passport" for item in data["items"])
    assert any(entry["category"] == "Food" for entry in data["finance_entries"])
    assert any(brief["brief_date"] == today for brief in data["briefs"])


def test_export_items_csv(client: TestClient) -> None:
    today = datetime.now(TZ).date().isoformat()
    queries.create_manual_item(today, "urgent", "Renew passport")

    response = client.get("/settings/export/items.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == "attachment; filename=items.csv"
    assert "Renew passport" in response.text
    assert response.text.startswith("id,")


def test_export_items_csv_empty_is_not_an_error(client: TestClient) -> None:
    with db.session() as conn:
        conn.execute("DELETE FROM items")
    response = client.get("/settings/export/items.csv")
    assert response.status_code == 200
    assert response.text == ""


def test_export_finances_csv(client: TestClient) -> None:
    today = datetime.now(TZ).date().isoformat()
    queries.create_finance_entry(42.5, "Food", "spend", today, note="Groceries")

    response = client.get("/settings/export/finances.csv")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == "attachment; filename=finances.csv"
    assert "Groceries" in response.text


def test_settings_page_shows_degraded_badge_on_the_affected_source(
    client: TestClient,
) -> None:
    from datetime import datetime

    from command_center.config import TZ

    today = datetime.now(TZ).date().isoformat()
    queries.save_triage_results(
        brief_date=today,
        triaged_items=[],
        calendar_events=[],
        degraded_sources=["gmail"],
        sources_attempted=["gmail"],
        force=False,
    )

    response = client.get("/settings")

    assert "Degraded today" in response.text
    assert "tooltipsEnabled" in response.text


def test_patch_source_config_updates_enabled_and_interval(client: TestClient) -> None:
    response = client.patch(
        "/settings/sources/tasks", json={"enabled": False, "interval_minutes": 15}
    )
    assert response.status_code == 200

    cfg = next(c for c in queries.list_source_configs() if c["source_name"] == "tasks")
    assert cfg["enabled"] == 0
    assert cfg["interval_minutes"] == 15


def test_patch_source_config_partial_update(client: TestClient) -> None:
    client.patch("/settings/sources/gmail", json={"interval_minutes": 120})
    cfg = next(c for c in queries.list_source_configs() if c["source_name"] == "gmail")
    assert cfg["interval_minutes"] == 120
    assert cfg["enabled"] == 1  # untouched


def test_patch_unknown_source_returns_404(client: TestClient) -> None:
    response = client.patch("/settings/sources/carrier-pigeon", json={"enabled": False})
    assert response.status_code == 404


def test_pull_now_skips_google_sources_without_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(name: str) -> None:
        raise AssertionError("run_source must not be called without Google credentials")

    monkeypatch.setattr(pipeline, "run_source", _explode)

    response = client.post("/settings/sources/gmail/pull", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


def test_pull_now_medium_does_not_require_google_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pipeline.medium,
        "fetch_and_rank",
        lambda: [
            {
                "lane": "reading",
                "source": "medium",
                "source_id": "a1",
                "title": "An article",
                "why_it_matters": "relevant",
                "suggested_next_step": "Read it",
                "priority": 2,
                "deep_link": "https://medium.com/a1",
            }
        ],
    )

    response = client.post("/settings/sources/medium/pull", follow_redirects=False)
    assert response.status_code == 303

    cfg = next(c for c in queries.list_source_configs() if c["source_name"] == "medium")
    assert cfg["last_pulled_at"] is not None

    brief = client.get("/brief")
    assert "An article" in brief.text


def test_pull_now_unknown_source_returns_404(client: TestClient) -> None:
    response = client.post("/settings/sources/carrier-pigeon/pull")
    assert response.status_code == 404
