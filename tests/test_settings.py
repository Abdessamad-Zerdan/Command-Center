from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, pipeline, queries
from command_center.assistant import ingest as assistant_ingest
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
