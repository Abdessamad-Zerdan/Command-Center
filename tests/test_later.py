""""Later · tomorrow": a snoozed task comes back on the next day's brief."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.config import TZ
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def _today() -> date:
    return datetime.now(TZ).date()


def _day(offset: int) -> str:
    return (_today() + timedelta(days=offset)).isoformat()


def _save(brief_date: str, source_id: str, title: str, priority: int = 2) -> int:
    queries.save_triage_results(
        brief_date=brief_date,
        triaged_items=[{
            "lane": "action_items", "source": "gmail", "source_id": source_id, "title": title,
            "why_it_matters": "", "suggested_next_step": "", "priority": priority, "deep_link": "",
        }],
        calendar_events=[],
        degraded_sources=[],
        sources_attempted=["gmail"],
    )
    with db.session() as conn:
        return conn.execute("SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchone()["id"]


def _row(item_id: int) -> dict:
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())


def _snooze_until(item_id: int, until: str | None) -> None:
    with db.session() as conn:
        conn.execute("UPDATE items SET status = 'snoozed', snoozed_until = ? WHERE id = ?", (until, item_id))


def test_later_hides_the_task_until_tomorrow(client: TestClient) -> None:
    item_id = _save(_day(0), "later", "Read the contract")

    assert client.post(f"/items/{item_id}/snooze").status_code == 200

    row = _row(item_id)
    assert (row["status"], row["snoozed_until"]) == ("snoozed", _day(1))
    assert "Read the contract" not in client.get("/brief").text


def test_due_task_wakes_onto_todays_brief_first_in_its_group(client: TestClient) -> None:
    _save(_day(0), "already-here", "Already on the brief")
    item_id = _save(_day(-1), "woke", "Call the dentist")
    _snooze_until(item_id, _day(0))

    page = client.get("/brief").text

    row = _row(item_id)
    assert (row["status"], row["brief_date"]) == ("pending", _day(0))
    assert "Back from Later" in page
    assert page.index("Call the dentist") < page.index("Already on the brief")


def test_task_not_due_yet_stays_asleep(client: TestClient) -> None:
    item_id = _save(_day(0), "not-yet", "Not until tomorrow")
    _snooze_until(item_id, _day(1))

    assert "Not until tomorrow" not in client.get("/brief").text
    assert _row(item_id)["status"] == "snoozed"


def test_old_undated_snooze_stays_asleep(client: TestClient) -> None:
    item_id = _save(_day(-3), "undated", "Snoozed before Later existed")
    _snooze_until(item_id, None)

    assert queries.wake_snoozed_items(_day(0)) == 0
    assert _row(item_id)["status"] == "snoozed"


def test_waking_never_creates_todays_brief(client: TestClient) -> None:
    # Startup only runs the morning pull when today's brief row is
    # missing — waking a task must not create it and so skip that pull.
    item_id = _save(_day(-1), "early", "Wakes too early")
    _snooze_until(item_id, _day(0))
    with db.session() as conn:  # items reference their brief, so clear them first
        conn.execute("DELETE FROM items WHERE brief_date = ?", (_day(0),))
        conn.execute("DELETE FROM briefs WHERE brief_date = ?", (_day(0),))

    assert queries.wake_snoozed_items(_day(0)) == 0
    assert _row(item_id)["status"] == "snoozed"
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM briefs WHERE brief_date = ?", (_day(0),)).fetchone() is None


def test_undo_brings_it_straight_back_without_the_pill(client: TestClient) -> None:
    item_id = _save(_day(0), "undo", "Changed my mind")
    client.post(f"/items/{item_id}/snooze")

    assert client.post(f"/items/{item_id}/reopen").status_code == 200

    row = _row(item_id)
    assert (row["status"], row["snoozed_until"]) == ("pending", None)
    page = client.get("/brief").text
    assert "Changed my mind" in page
    assert "Back from Later" not in page


def test_lanes_partial_wakes_today_but_not_past_days(client: TestClient) -> None:
    item_id = _save(_day(-2), "partial", "Pay the invoice")
    _snooze_until(item_id, _day(-1))

    past = client.get(f"/brief/lanes-partial?date={_day(-1)}")
    assert _row(item_id)["status"] == "snoozed"

    today = client.get("/brief/lanes-partial").json()
    assert past.status_code in (200, 404)
    assert "Pay the invoice" in today["html"]


def test_later_refuses_a_foreign_host(client: TestClient) -> None:
    item_id = _save(_day(0), "guard", "Guarded")

    response = client.post(f"/items/{item_id}/snooze", headers={"host": "evil.example.com"})

    assert response.status_code == 400
    assert _row(item_id)["status"] == "pending"
