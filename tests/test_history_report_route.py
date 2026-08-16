from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, queries
from command_center.assistant import ingest
from command_center.config import TZ
from command_center.history import router as history_router
from command_center.setup_wizard import status as setup_status
from command_center.triage import TriageProviderError


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(ingest, "rebuild_index", lambda *a, **k: False)
    # Default: never hit real Groq during route tests — reflection
    # generation is tested in isolation in test_reflection_*.py. Tests
    # that care about the real degrade/regenerate behavior override this
    # per-test via monkeypatch below.
    monkeypatch.setattr(
        history_router.reflection, "get_or_generate_reflection", lambda *a, **k: "Mock reflection text."
    )
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def test_history_report_does_not_get_shadowed_by_history_brief_date_route(client: TestClient) -> None:
    # The critical regression test: /history/{brief_date} is a single-
    # segment path param that would also match "/history/report" if it
    # were registered first in Starlette's routing table (capturing
    # brief_date="report"), producing history_detail's 404 ("No brief for
    # that date") instead of the real report page.
    response = client.get("/history/report")

    assert response.status_code == 200
    assert "Report" in response.text
    assert "No brief for that date" not in response.text


def test_history_report_defaults_to_week_period(client: TestClient) -> None:
    response = client.get("/history/report")
    assert response.status_code == 200
    # active-tab pill styling only applies to the current period
    assert "bg-rust" in response.text


@pytest.mark.parametrize(
    "query",
    [
        "?period=day&date=2026-08-14",
        "?period=week&date=2026-08-14",
        "?period=month&date=2026-08-01",
        "?period=year&date=2026-01-01",
    ],
)
def test_history_report_renders_for_every_period_with_no_data(client: TestClient, query: str) -> None:
    # Empty state must not 500 — exercises the chart functions' zero-max guards.
    response = client.get(f"/history/report{query}")
    assert response.status_code == 200
    # base.html's own icons (dark-mode toggle, settings gear) also use
    # inline <svg> — just confirm both charts rendered on top of those.
    assert response.text.count("<svg") >= 2


def test_history_report_rejects_unknown_period(client: TestClient) -> None:
    response = client.get("/history/report?period=fortnight")
    assert response.status_code == 400


def test_history_report_rejects_invalid_date(client: TestClient) -> None:
    response = client.get("/history/report?date=not-a-date")
    assert response.status_code == 400


def test_history_report_prev_next_week_are_exactly_seven_days_apart(client: TestClient) -> None:
    response = client.get("/history/report?period=week&date=2026-08-14")
    assert response.status_code == 200
    assert "date=2026-08-07" in response.text  # prev
    assert "date=2026-08-21" in response.text  # next


def test_history_report_reflects_real_activity(client: TestClient) -> None:
    # log_task_event always stamps the real current time, so the "day"
    # window queried here must be the real "today" — not a fixed date —
    # or the event falls outside the query range once the calendar day
    # rolls over.
    today = datetime.now(TZ).date().isoformat()
    item_id = queries.create_manual_item(today, "urgent", "Ship the thing")
    queries.set_item_status(item_id, "done")

    response = client.get(f"/history/report?period=day&date={today}")

    assert response.status_code == 200
    # stat cards: Created and Completed both reflect the one item.
    assert response.text.count(">1</div>") >= 2


def test_existing_history_list_route_still_works(client: TestClient) -> None:
    response = client.get("/history")
    assert response.status_code == 200


def test_existing_history_detail_route_still_works(client: TestClient) -> None:
    queries.create_manual_item("2020-01-01", "urgent", "Old task")
    response = client.get("/history/2020-01-01")
    assert response.status_code == 200

    response_missing = client.get("/history/2019-01-01")
    assert response_missing.status_code == 404


# --- reflection callout ---------------------------------------------------


def test_reflection_callout_renders_for_week_period(client: TestClient) -> None:
    response = client.get("/history/report?period=week&date=2026-08-14")

    assert response.status_code == 200
    assert "Reflection" in response.text
    assert "Mock reflection text." in response.text
    assert "Regenerate" in response.text


def test_reflection_callout_absent_for_day_period(client: TestClient) -> None:
    response = client.get("/history/report?period=day&date=2026-08-14")

    assert response.status_code == 200
    assert "Regenerate" not in response.text


def test_reflection_degrades_to_unavailable_message_on_provider_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*a, **k):
        raise TriageProviderError("Groq request failed: authentication error.")

    monkeypatch.setattr(history_router.reflection, "get_or_generate_reflection", _raise)

    response = client.get("/history/report?period=week&date=2026-08-14")

    assert response.status_code == 200
    assert "Reflection unavailable right now." in response.text


def test_reflection_text_renders_escaped_not_as_live_html(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        history_router.reflection, "get_or_generate_reflection", lambda *a, **k: "<script>alert(1)</script>"
    )

    response = client.get("/history/report?period=week&date=2026-08-14")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_regenerate_reflection_calls_with_force_true_and_redirects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    def _fake(period, start, end, force=False):
        captured["period"] = period
        captured["force"] = force
        return "ok"

    monkeypatch.setattr(history_router.reflection, "get_or_generate_reflection", _fake)

    response = client.post(
        "/history/report/regenerate-reflection",
        data={"period": "week", "date": "2026-08-14"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/history/report?period=week&date=2026-08-14"
    assert captured == {"period": "week", "force": True}


def test_regenerate_reflection_rejects_day_period(client: TestClient) -> None:
    response = client.post(
        "/history/report/regenerate-reflection", data={"period": "day", "date": "2026-08-14"}
    )
    assert response.status_code == 400


def test_regenerate_reflection_swallows_provider_error_and_still_redirects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*a, **k):
        raise TriageProviderError("boom")

    monkeypatch.setattr(history_router.reflection, "get_or_generate_reflection", _raise)

    response = client.post(
        "/history/report/regenerate-reflection",
        data={"period": "week", "date": "2026-08-14"},
        follow_redirects=False,
    )

    assert response.status_code == 303


# --- sidebar stats ----------------------------------------------------------


def test_sidebar_stats_render_for_week_period(client: TestClient) -> None:
    item_id = queries.create_manual_item("2026-08-14", "urgent", "Ship the thing")
    queries.set_item_status(item_id, "done")

    response = client.get("/history/report?period=week&date=2026-08-14")

    assert response.status_code == 200
    assert "Current streak" in response.text
    assert "All-time completed" in response.text
    assert "Best week" in response.text
    assert "Vs. previous week" in response.text


def test_sidebar_best_period_omitted_for_day_view(client: TestClient) -> None:
    response = client.get("/history/report?period=day&date=2026-08-14")

    assert response.status_code == 200
    assert "Current streak" in response.text
    assert "All-time completed" in response.text
    assert "Vs. previous day" in response.text
    assert "Best day" not in response.text


def test_sidebar_reflects_real_completion_counts(client: TestClient) -> None:
    today = datetime.now(TZ).date().isoformat()
    item_id = queries.create_manual_item(today, "urgent", "Ship the thing")
    queries.set_item_status(item_id, "done")

    response = client.get(f"/history/report?period=day&date={today}")

    assert response.status_code == 200
    assert "1 day" in response.text  # current streak, singular
