from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.config import PROFILE, TZ
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Point the module-level DB_PATH at a scratch file so tests never touch
    # the real command_center.db.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    # Force fixture-mode regardless of whether this machine has real Google
    # credentials on disk — tests must never trigger a real Gmail/Calendar/
    # triage call via the app's startup lifespan.
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    # Isolate from the setup wizard's gating middleware — these tests
    # exercise the main app, not the wizard, and shouldn't need real
    # config to reach it.
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    # Never let the app's startup lifespan load the real embedding model —
    # slow and possibly network-dependent, and irrelevant to these tests.
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def test_home_renders_portfolio(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert PROFILE["name"] in response.text
    assert "Today's Brief" in response.text
    # Temporarily removed pending a different approach
    assert "Current Projects" not in response.text
    assert "Build Log" not in response.text


def test_brief_renders_fixture_lanes(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert "Daily Command Center" in response.text
    assert "Production alert: payment webhook failing" in response.text


def test_brief_shows_demo_data_banner_for_fixture_seeded_brief(client: TestClient) -> None:
    # This app's fixture data is realistic-looking (fake client emails,
    # fake meetings) — it must never render indistinguishably from a real
    # pull, or it reads as a data leak rather than a demo.
    response = client.get("/brief")
    assert response.status_code == 200
    assert "Showing sample data" in response.text


def test_brief_shows_friendly_not_ready_page_instead_of_a_500(client: TestClient) -> None:
    today = datetime.now(TZ).date().isoformat()
    with db.session() as conn:
        conn.execute("DELETE FROM items WHERE brief_date = ?", (today,))
        conn.execute("DELETE FROM briefs WHERE brief_date = ?", (today,))

    response = client.get("/brief")

    assert response.status_code == 200
    assert "hasn't been generated yet" in response.text
    assert 'action="/rerun"' in response.text


def test_brief_omits_demo_data_banner_once_real_results_are_saved(client: TestClient) -> None:
    today = datetime.now(TZ).date().isoformat()
    queries.save_triage_results(
        brief_date=today,
        triaged_items=[],
        calendar_events=[],
        degraded_sources=[],
        sources_attempted=["gmail"],
        force=False,
    )
    response = client.get("/brief")
    assert response.status_code == 200
    assert "Showing sample data" not in response.text


def test_mark_done_removes_item_and_persists(client: TestClient) -> None:
    with db.session() as conn:
        item_id = conn.execute(
            "SELECT id FROM items WHERE title LIKE 'Production alert%'"
        ).fetchone()["id"]

    done_response = client.post(f"/items/{item_id}/done")
    assert done_response.status_code == 200

    dashboard = client.get("/brief")
    assert "Production alert: payment webhook failing" not in dashboard.text

    with db.session() as conn:
        status = conn.execute(
            "SELECT status FROM items WHERE id = ?", (item_id,)
        ).fetchone()["status"]
    assert status == "done"


def _seed_past_item(brief_date: str, lane: str, title: str) -> int:
    with db.session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO briefs (brief_date, generated_at, degraded_lanes) VALUES (?, ?, '[]')",
            (brief_date, "2026-08-10T10:00:00"),
        )
        cursor = conn.execute(
            "INSERT INTO items (brief_date, lane, source, source_id, title, why_it_matters, "
            "suggested_next_step, priority, deep_link, status, created_at) "
            "VALUES (?, ?, 'google_tasks', ?, ?, '', '', 2, '', 'pending', ?)",
            (brief_date, lane, f"src-{title}", title, "2026-08-10T10:00:00"),
        )
        return cursor.lastrowid


def test_move_item_to_today_route_moves_it_off_history_and_onto_todays_brief(
    client: TestClient,
) -> None:
    item_id = _seed_past_item("2026-08-10", "action_items", "Old task from history")

    response = client.post(f"/items/{item_id}/move-to-today")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    history = client.get("/history/2026-08-10")
    assert "Old task from history" not in history.text

    today = datetime.now(TZ).date().isoformat()
    with db.session() as conn:
        row = conn.execute("SELECT brief_date, status FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["brief_date"] == today
    assert row["status"] == "pending"


def test_move_item_to_today_route_404s_for_unknown_item(client: TestClient) -> None:
    response = client.post("/items/999999/move-to-today")
    assert response.status_code == 404


def test_history_item_card_shows_bring_to_today_button(client: TestClient) -> None:
    _seed_past_item("2026-08-10", "action_items", "Old task from history")
    response = client.get("/history/2026-08-10")
    assert "Bring to today" in response.text


def test_todays_brief_does_not_show_bring_to_today_button(client: TestClient) -> None:
    response = client.get("/brief")
    assert "Bring to today" not in response.text


def test_history_lists_today(client: TestClient) -> None:
    response = client.get("/history")
    assert response.status_code == 200
    assert "today" in response.text


def test_create_item_adds_to_lane(client: TestClient) -> None:
    response = client.post("/items", json={"lane": "urgent", "title": "Call the bank"})
    assert response.status_code == 200
    item_id = response.json()["id"]
    assert isinstance(item_id, int)

    dashboard = client.get("/brief")
    assert "Call the bank" in dashboard.text
    assert "MANUAL" in dashboard.text.upper()


def test_create_item_with_due_date_persists_it(client: TestClient) -> None:
    response = client.post(
        "/items", json={"lane": "urgent", "title": "Renew passport", "due_date": "2026-08-21"}
    )
    assert response.status_code == 200
    item_id = response.json()["id"]

    with db.session() as conn:
        row = conn.execute("SELECT due_date FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["due_date"] == "2026-08-21"


def test_create_item_without_due_date_defaults_to_none(client: TestClient) -> None:
    response = client.post("/items", json={"lane": "urgent", "title": "No due date"})
    item_id = response.json()["id"]

    with db.session() as conn:
        row = conn.execute("SELECT due_date FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["due_date"] is None


def test_create_item_rejects_unknown_lane(client: TestClient) -> None:
    response = client.post("/items", json={"lane": "not_a_lane", "title": "x"})
    assert response.status_code == 400


def test_create_item_rejects_empty_title(client: TestClient) -> None:
    response = client.post("/items", json={"lane": "urgent", "title": "   "})
    assert response.status_code == 400


def test_create_manual_item_defaults(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-08-13", "tasks_due", "Renew passport")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["source"] == "manual"
    assert row["title"] == "Renew passport"
    assert row["priority"] == 2
    assert row["status"] == "pending"
    assert row["why_it_matters"] == ""
    assert row["deep_link"] == ""


def test_manual_item_card_has_no_broken_open_link(client: TestClient) -> None:
    client.post("/items", json={"lane": "action_items", "title": "Review budget"})
    dashboard = client.get("/brief")
    # The manual item's card should render without a deep_link href pointing
    # nowhere — no href="" for the Open link.
    assert 'href=""' not in dashboard.text


def test_update_title_succeeds_for_manual_item(client: TestClient) -> None:
    create = client.post("/items", json={"lane": "urgent", "title": "Original title"})
    item_id = create.json()["id"]

    response = client.patch(f"/items/{item_id}/title", json={"title": "Renamed title"})
    assert response.status_code == 200

    with db.session() as conn:
        title = conn.execute(
            "SELECT title FROM items WHERE id = ?", (item_id,)
        ).fetchone()["title"]
    assert title == "Renamed title"


def test_update_title_rejected_for_non_manual_item(client: TestClient) -> None:
    with db.session() as conn:
        item_id = conn.execute(
            "SELECT id FROM items WHERE title LIKE 'Production alert%'"
        ).fetchone()["id"]

    response = client.patch(f"/items/{item_id}/title", json={"title": "Hacked title"})
    assert response.status_code == 403

    with db.session() as conn:
        title = conn.execute(
            "SELECT title FROM items WHERE id = ?", (item_id,)
        ).fetchone()["title"]
    assert "Production alert" in title


def test_update_title_rejects_empty(client: TestClient) -> None:
    create = client.post("/items", json={"lane": "urgent", "title": "Keep me"})
    item_id = create.json()["id"]
    response = client.patch(f"/items/{item_id}/title", json={"title": "   "})
    assert response.status_code == 400


def test_update_item_lane_moves_a_triaged_item(client: TestClient) -> None:
    with db.session() as conn:
        item_id = conn.execute(
            "SELECT id FROM items WHERE title LIKE 'Production alert%'"
        ).fetchone()["id"]

    response = client.patch(f"/items/{item_id}/lane", json={"lane": "action_items"})
    assert response.status_code == 200

    with db.session() as conn:
        lane = conn.execute("SELECT lane FROM items WHERE id = ?", (item_id,)).fetchone()["lane"]
    assert lane == "action_items"


def test_update_item_lane_accepts_reading_as_a_valid_target(client: TestClient) -> None:
    create = client.post("/items", json={"lane": "urgent", "title": "Move me to reading"})
    item_id = create.json()["id"]

    response = client.patch(f"/items/{item_id}/lane", json={"lane": "reading"})
    assert response.status_code == 200

    dashboard = client.get("/brief")
    assert "Move me to reading" in dashboard.text


def test_update_item_lane_rejects_unknown_lane(client: TestClient) -> None:
    create = client.post("/items", json={"lane": "urgent", "title": "x"})
    item_id = create.json()["id"]
    response = client.patch(f"/items/{item_id}/lane", json={"lane": "not_a_lane"})
    assert response.status_code == 400


def test_update_item_lane_rejects_unknown_item(client: TestClient) -> None:
    response = client.patch("/items/999999/lane", json={"lane": "urgent"})
    assert response.status_code == 404


def test_update_item_project_assigns_project(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    create = client.post("/items", json={"lane": "urgent", "title": "Link me"})
    item_id = create.json()["id"]

    response = client.patch(f"/items/{item_id}/project", json={"project_id": project_id})

    assert response.status_code == 200
    with db.session() as conn:
        row = conn.execute("SELECT project_id FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["project_id"] == project_id


def test_update_item_project_can_clear_to_none(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))
    create = client.post("/items", json={"lane": "urgent", "title": "Unlink me"})
    item_id = create.json()["id"]
    client.patch(f"/items/{item_id}/project", json={"project_id": project_id})

    response = client.patch(f"/items/{item_id}/project", json={"project_id": None})

    assert response.status_code == 200
    with db.session() as conn:
        row = conn.execute("SELECT project_id FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["project_id"] is None


def test_update_item_project_rejects_unknown_item(client: TestClient) -> None:
    response = client.patch("/items/999999/project", json={"project_id": None})
    assert response.status_code == 404


def test_dashboard_includes_project_picker_when_active_projects_exist(
    client: TestClient, tmp_path: Path
) -> None:
    queries.create_registered_project("Foo", str(tmp_path))
    response = client.get("/brief")
    assert response.status_code == 200
    assert "saveProject()" in response.text
    assert "Foo" in response.text
    # Regression guard: a failed PATCH must revert the dropdown instead
    # of leaving it showing an unsaved selection as if it had saved.
    assert "this.projectId = this.savedProjectId" in response.text


def test_item_card_snooze_and_done_only_hide_the_card_on_success(client: TestClient) -> None:
    # Regression guard: these used to hide the card and decrement the
    # lane counter immediately, before the fetch even resolved — a failed
    # request looked identical to a successful one.
    response = client.get("/brief")
    assert response.status_code == 200
    assert ".then(r => { if (r.ok) { removed = true;" in response.text


def test_dashboard_item_cards_still_keep_the_height_cap(client: TestClient) -> None:
    # full_height is only set true in project_detail.html's Tasks section —
    # /brief's own cards must keep the existing max-h-[180px] cap unchanged.
    response = client.get("/brief")
    assert response.status_code == 200
    assert "max-h-[180px]" in response.text


def test_header_icons_have_hover_tooltips(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    # Only the 2 always-visible header controls get a hover tooltip: the
    # nav rail's trigger ("Menu") and the dark-mode toggle. The rail's own
    # items show their label inline while open and don't need one.
    for label in ("Menu", "Toggle theme"):
        assert f'class="icon-tooltip' in response.text
        assert f">{label}</span>" in response.text
    assert response.text.count('class="icon-tooltip') == 2


def test_tooltip_disabled_class_toggle_script_present(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert "tooltipsEnabled" in response.text
    assert "tooltips-disabled" in response.text


def test_dashboard_shows_priority_group_headers_only_when_mixed(client: TestClient) -> None:
    dashboard = client.get("/brief")
    # meeting_prep fixture data has priority 1 and priority 3 items — mixed,
    # so it should be split into labeled groups.
    assert "High priority" in dashboard.text
    assert "Low priority" in dashboard.text
    # tasks_due fixture data has priority 2 and priority 3 — also mixed.
    assert "Medium priority" in dashboard.text
