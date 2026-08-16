from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, queries
from command_center.assistant import ingest
from command_center.finances import aggregations, charts
from command_center.finances import router as finances_router
from command_center.setup_wizard import status as setup_status
from command_center.triage import TriageProviderError


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(ingest, "rebuild_index", lambda *a, **k: False)
    # Never hit real Groq during route tests — reflection generation
    # logic is covered in isolation below.
    monkeypatch.setattr(
        finances_router.reflection, "get_or_generate_reflection", lambda *a, **k: "Mock reflection text."
    )
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


# --- schema / queries CRUD --------------------------------------------------


def test_create_finance_entry_persists_and_lists(client: TestClient) -> None:
    queries.create_finance_entry(42.5, "Food", "spend", "2026-08-10", note="Groceries")
    entries = queries.list_finance_entries("2026-08-01", "2026-09-01")
    assert len(entries) == 1
    assert entries[0]["amount"] == 42.5
    assert entries[0]["category"] == "Food"
    assert entries[0]["type"] == "spend"
    assert entries[0]["note"] == "Groceries"


def test_list_finance_entries_respects_half_open_bounds(client: TestClient) -> None:
    queries.create_finance_entry(10, "Rent", "spend", "2026-07-31")
    queries.create_finance_entry(20, "Rent", "spend", "2026-08-01")
    queries.create_finance_entry(30, "Rent", "spend", "2026-08-31")
    queries.create_finance_entry(40, "Rent", "spend", "2026-09-01")

    entries = queries.list_finance_entries("2026-08-01", "2026-09-01")
    amounts = {e["amount"] for e in entries}
    assert amounts == {20, 30}


# --- aggregations ------------------------------------------------------------


def test_monthly_summary_buckets_by_type(client: TestClient) -> None:
    queries.create_finance_entry(100, "Rent", "spend", "2026-08-01")
    queries.create_finance_entry(50, "Food", "spend", "2026-08-05")
    queries.create_finance_entry(500, "Other", "income", "2026-08-10")
    queries.create_finance_entry(30, "Savings", "saving", "2026-08-15")

    from datetime import date

    summary = aggregations.monthly_summary(date(2026, 8, 1), date(2026, 9, 1))
    assert summary["total_spent"] == 150
    assert summary["total_income"] == 500
    assert summary["total_saved"] == 30
    assert summary["net_delta"] == 350


def test_category_breakdown_excludes_income_and_saving(client: TestClient) -> None:
    queries.create_finance_entry(100, "Rent", "spend", "2026-08-01")
    queries.create_finance_entry(500, "Other", "income", "2026-08-10")
    queries.create_finance_entry(30, "Savings", "saving", "2026-08-15")

    from datetime import date

    breakdown = aggregations.category_breakdown(date(2026, 8, 1), date(2026, 9, 1))
    assert breakdown == {"Rent": 100}


def test_monthly_trend_zero_fills_and_covers_trailing_months(client: TestClient) -> None:
    queries.create_finance_entry(100, "Rent", "spend", "2026-08-01")
    queries.create_finance_entry(40, "Food", "spend", "2026-06-15")

    from datetime import date

    trend = aggregations.monthly_trend(date(2026, 8, 1), num_months=3)
    assert [b["bucket"] for b in trend] == ["Jun", "Jul", "Aug"]
    assert [b["count"] for b in trend] == [40, 0, 100]


# --- chart rendering (empty-state div/0 guards) ------------------------------


def test_render_category_bars_svg_handles_empty_breakdown() -> None:
    svg = charts.render_category_bars_svg({})
    assert svg.startswith("<svg")


def test_render_trend_svg_handles_all_zero_buckets() -> None:
    svg = charts.render_trend_svg([{"bucket": "Jan", "count": 0}, {"bucket": "Feb", "count": 0}])
    assert svg.startswith("<svg")


# --- route ---------------------------------------------------------------


def test_finances_route_renders_with_no_data(client: TestClient) -> None:
    response = client.get("/finances")
    assert response.status_code == 200
    assert "Finances" in response.text
    assert "No spending recorded this month." in response.text
    assert "No entries yet this month." in response.text


def test_finances_route_rejects_invalid_date(client: TestClient) -> None:
    response = client.get("/finances?date=not-a-date")
    assert response.status_code == 400


def test_finances_route_prev_next_are_adjacent_months(client: TestClient) -> None:
    response = client.get("/finances?date=2026-08-14")
    assert response.status_code == 200
    # shift_anchor("month", ..., -1) returns a date *inside* the previous
    # month (its last day), same convention history/periods.py uses —
    # period_bounds re-derives that month's own start from it.
    assert "date=2026-07-31" in response.text  # prev
    assert "date=2026-09-01" in response.text  # next


def test_finances_route_reflects_real_entries(client: TestClient) -> None:
    queries.create_finance_entry(75, "Food", "spend", "2026-08-05", note="Lunch")
    response = client.get("/finances?date=2026-08-01")

    assert response.status_code == 200
    assert "75.00" in response.text
    assert "Lunch" in response.text
    assert "No entries yet this month." not in response.text


def test_finances_route_not_in_header_but_in_nav_rail(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    # The only always-visible, standalone-fixed header controls are the
    # rail trigger ("Menu") and the dark-mode toggle. Finances only gets
    # an aria-label as one of the rail's own icon-only items (the rail
    # itself is always in the DOM, opened via hover/tap) — not as a
    # separate top-level fixed icon the way it briefly was in the old
    # More dropdown's predecessor design.
    assert 'class="group fixed top-4 right-36' not in response.text
    assert 'href="/finances"' in response.text


def test_finances_is_the_active_nav_rail_item_on_its_own_page(client: TestClient) -> None:
    response = client.get("/finances")
    assert response.status_code == 200
    assert "bg-[#faece7]" in response.text


# --- entry creation ---------------------------------------------------------


def test_post_entry_creates_and_redirects(client: TestClient) -> None:
    response = client.post(
        "/finances/entries",
        data={
            "amount": "12.50",
            "category": "Food",
            "type": "spend",
            "entry_date": "2026-08-05",
            "note": "Coffee",
            "date": "2026-08-01",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/finances?date=2026-08-01"

    entries = queries.list_finance_entries("2026-08-01", "2026-09-01")
    assert len(entries) == 1
    assert entries[0]["amount"] == 12.5
    assert entries[0]["note"] == "Coffee"


def test_post_entry_rejects_unknown_category(client: TestClient) -> None:
    response = client.post(
        "/finances/entries",
        data={
            "amount": "10",
            "category": "Yacht",
            "type": "spend",
            "entry_date": "2026-08-05",
            "date": "2026-08-01",
        },
    )
    assert response.status_code == 400


def test_post_entry_rejects_unknown_type(client: TestClient) -> None:
    response = client.post(
        "/finances/entries",
        data={
            "amount": "10",
            "category": "Food",
            "type": "yolo",
            "entry_date": "2026-08-05",
            "date": "2026-08-01",
        },
    )
    assert response.status_code == 400


def test_post_entry_rejects_non_positive_amount(client: TestClient) -> None:
    response = client.post(
        "/finances/entries",
        data={
            "amount": "0",
            "category": "Food",
            "type": "spend",
            "entry_date": "2026-08-05",
            "date": "2026-08-01",
        },
    )
    assert response.status_code == 400


def test_post_entry_rejects_invalid_date(client: TestClient) -> None:
    response = client.post(
        "/finances/entries",
        data={
            "amount": "10",
            "category": "Food",
            "type": "spend",
            "entry_date": "not-a-date",
            "date": "2026-08-01",
        },
    )
    assert response.status_code == 400


# --- reflection callout ---------------------------------------------------


def test_reflection_callout_renders(client: TestClient) -> None:
    response = client.get("/finances?date=2026-08-01")
    assert response.status_code == 200
    assert "Reflection" in response.text
    assert "Mock reflection text." in response.text
    assert "Regenerate" in response.text


def test_reflection_degrades_to_unavailable_message_on_provider_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*a, **k):
        raise TriageProviderError("Groq request failed: authentication error.")

    monkeypatch.setattr(finances_router.reflection, "get_or_generate_reflection", _raise)

    response = client.get("/finances?date=2026-08-01")

    assert response.status_code == 200
    assert "Reflection unavailable right now." in response.text


def test_regenerate_reflection_calls_with_force_true_and_redirects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    def _fake(start, end, force=False):
        captured["force"] = force
        return "ok"

    monkeypatch.setattr(finances_router.reflection, "get_or_generate_reflection", _fake)

    response = client.post(
        "/finances/regenerate-reflection", data={"date": "2026-08-14"}, follow_redirects=False
    )

    assert response.status_code == 303
    # Redirects back to the raw submitted anchor, unnormalized — same
    # pattern history/router.py's own regenerate route uses.
    assert response.headers["location"] == "/finances?date=2026-08-14"
    assert captured == {"force": True}


def test_regenerate_reflection_swallows_provider_error_and_still_redirects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*a, **k):
        raise TriageProviderError("boom")

    monkeypatch.setattr(finances_router.reflection, "get_or_generate_reflection", _raise)

    response = client.post(
        "/finances/regenerate-reflection", data={"date": "2026-08-14"}, follow_redirects=False
    )

    assert response.status_code == 303
