from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, queries
from command_center.assistant import ingest
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


# --- old header/dropdown fully removed --------------------------------------


def test_old_three_icon_header_and_more_dropdown_are_gone(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    # "More" was only ever the old dropdown trigger's label — fully
    # replaced by "Menu". Brief/Projects labels are legitimately reused
    # as accessible names on the new icon-only rail items, so this checks
    # for the OLD standalone fixed-position icon markup specifically,
    # not mere presence of those label strings anywhere on the page.
    assert 'aria-label="More"' not in response.text
    assert 'class="group fixed top-4 right-36' not in response.text
    assert 'class="group fixed top-4 right-24' not in response.text


# --- trigger + full item list -------------------------------------------------


def test_nav_rail_trigger_present_once(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert response.text.count('aria-label="Menu"') == 1


def test_nav_rail_has_all_nine_items(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    for href in (
        "/brief",
        "/calendar",
        "/map",
        "/projects",
        "/schedule",
        "/history",
        "/history/report",
        "/finances",
        "/settings",
    ):
        assert f'href="{href}"' in response.text
    for label in ("Brief", "Calendar", "Map", "Projects", "Schedule", "History", "Report", "Finances", "Settings"):
        assert f">{label}</span>" in response.text


def test_dark_mode_toggle_still_separate_from_rail(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert 'aria-label="Toggle dark mode"' in response.text


def test_rail_opens_on_click_unconditionally_no_hover_mechanism(client: TestClient) -> None:
    # Hover-to-open (desktop) was tried and dropped — it kept closing
    # before the pointer reached the rail. Click/tap now opens it on
    # every device; this guards against the touch-only matchMedia gate
    # (or any other hover-driven open mechanism) creeping back in.
    response = client.get("/brief")
    assert response.status_code == 200
    assert '@click="open = !open"' in response.text
    assert "hover: none" not in response.text
    assert "@media (hover: hover)" not in response.text


# --- icon-only rail, per-item hover labels ------------------------------------


def test_rail_items_are_icon_only_with_a_per_item_hover_label(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    # A distinct class from the header's own icon-tooltip — per-item rail
    # labels are a different, always-on mechanism (see next test), not the
    # user-toggleable header tooltip feature.
    assert response.text.count('class="nav-item-tooltip') == 9


def test_rail_item_labels_are_not_gated_by_the_tooltip_settings_toggle(client: TestClient) -> None:
    # html.tooltips-disabled only hides .icon-tooltip (the header's Menu/
    # Toggle-theme hints) — an icon-only rail needs its per-item labels to
    # keep working even if the user has turned that setting off, since
    # they're the only way to identify an icon without opening its page.
    response = client.get("/brief")
    assert response.status_code == 200
    assert "html.tooltips-disabled .nav-item-tooltip" not in response.text


def test_rail_slides_out_horizontally_to_the_left_of_the_trigger(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert 'class="nav-rail shadow-float absolute right-full top-0 mr-2 flex items-center gap-1.5' in response.text


def test_rail_has_a_solid_background_behind_its_icons(client: TestClient) -> None:
    # Regression guard: the rail must render as a distinct panel, not a
    # bare row of icons floating over whatever page content is beneath —
    # a real user-reported confusion the individual icons' own
    # semi-transparent pills didn't fully solve. bg-white/90 + backdrop-blur
    # is still an opaque-reading panel (glassmorphism, not a bare row).
    response = client.get("/brief")
    assert response.status_code == 200
    assert (
        'class="nav-rail shadow-float absolute right-full top-0 mr-2 flex items-center gap-1.5 '
        'rounded-full border border-stone-200 dark:border-stone-700 '
        'bg-white/90 dark:bg-stone-800/90 backdrop-blur-md px-1.5 py-1.5"'
    ) in response.text


# --- active-item indicator ---------------------------------------------------


def test_brief_is_active_on_brief_page(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert response.text.count("bg-[#faece7]") == 1


def test_no_item_is_active_on_the_portfolio_home_page(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "bg-[#faece7]" not in response.text


def test_projects_is_active_on_projects_list_and_detail(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Foo", str(tmp_path))

    list_response = client.get("/projects")
    assert list_response.text.count("bg-[#faece7]") == 1

    detail_response = client.get(f"/projects/{project_id}")
    assert detail_response.text.count("bg-[#faece7]") == 1


def test_report_is_active_not_history_on_report_page(client: TestClient) -> None:
    response = client.get("/history/report")
    assert response.status_code == 200
    # Only one item lit up — Report, not History, even though the path
    # starts with "/history/".
    assert response.text.count("bg-[#faece7]") == 1


def test_history_is_active_on_history_list_and_detail_but_not_report(client: TestClient) -> None:
    queries.create_manual_item("2020-01-01", "urgent", "Old task")

    list_response = client.get("/history")
    assert list_response.text.count("bg-[#faece7]") == 1

    detail_response = client.get("/history/2020-01-01")
    assert detail_response.text.count("bg-[#faece7]") == 1


def test_settings_is_active_on_settings_and_settings_schedule(client: TestClient) -> None:
    settings_response = client.get("/settings")
    assert settings_response.text.count("bg-[#faece7]") == 1

    schedule_response = client.get("/settings/schedule")
    assert schedule_response.text.count("bg-[#faece7]") == 1


def test_finances_is_active_on_finances_page(client: TestClient) -> None:
    response = client.get("/finances")
    assert response.status_code == 200
    assert response.text.count("bg-[#faece7]") == 1


# --- command palette -----------------------------------------------------------


def test_command_palette_renders_on_every_page(client: TestClient) -> None:
    for path in ("/brief", "/settings", "/finances"):
        response = client.get(path)
        assert response.status_code == 200
        assert "commandPalette()" in response.text
        assert "Jump to..." in response.text


def test_command_palette_listens_for_cmd_or_ctrl_k(client: TestClient) -> None:
    response = client.get("/brief")
    assert "e.metaKey || e.ctrlKey" in response.text
    assert "'k'" in response.text


def test_command_palette_covers_every_nav_rail_destination(client: TestClient) -> None:
    # Anywhere reachable from the nav rail should also be reachable from
    # the palette — otherwise it's a keyboard shortcut to a subset of
    # the app, which defeats the point.
    response = client.get("/brief")
    for href in ("/brief", "/projects", "/schedule", "/history", "/history/report", "/finances", "/settings"):
        assert f"href: '{href}'" in response.text
