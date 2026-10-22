"""Still-open items from earlier days on today's brief (carryover.py)."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, carryover, db, queries
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


def _days_ago(n: int) -> str:
    return (_today() - timedelta(days=n)).isoformat()


def _save(
    brief_date: str, source_id: str, title: str, lane: str = "action_items", source: str = "gmail"
) -> int:
    queries.save_triage_results(
        brief_date=brief_date,
        triaged_items=[{
            "lane": lane, "source": source, "source_id": source_id, "title": title,
            "why_it_matters": "", "suggested_next_step": "", "priority": 2, "deep_link": "",
        }],
        calendar_events=[],
        degraded_sources=[],
        sources_attempted=["gmail"],
    )
    with db.session() as conn:
        return conn.execute("SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchone()["id"]


def _set_status(item_id: int, status: str) -> None:
    with db.session() as conn:
        conn.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))


def test_list_carried_over_keeps_only_open_work_from_the_window(client: TestClient) -> None:
    today = _today().isoformat()
    _save(_days_ago(1), "yesterday", "Yesterday's open task")
    _save(_days_ago(14), "edge", "Fourteen days old")
    _save(_days_ago(15), "too-old", "Fifteen days old")
    _save(_days_ago(2), "reading", "A newsletter", lane="reading")
    _set_status(_save(_days_ago(2), "done", "Already done"), "done")
    _set_status(_save(_days_ago(2), "dismissed", "Dismissed one"), "dismissed")
    _set_status(_save(_days_ago(2), "snoozed", "Snoozed one"), "snoozed")
    _save(today, "today", "Today's own task")

    titles = [i["title"] for i in queries.list_carried_over(today, 14)]

    assert titles == ["Yesterday's open task", "Fourteen days old"]  # newest day first


def test_calendar_items_carry_over_only_from_yesterday(client: TestClient) -> None:
    # A pull fetches today's and tomorrow's events, so yesterday's
    # "tomorrow" may be today; anything older has already happened.
    _save(_days_ago(1), "cal-1", "Standup tomorrow", lane="meeting_prep", source="calendar")
    _save(_days_ago(2), "cal-2", "Event already over", lane="meeting_prep", source="calendar")

    titles = [i["title"] for i in queries.list_carried_over(_today().isoformat(), 14)]

    assert titles == ["Standup tomorrow"]


def test_brief_lists_earlier_open_items_with_their_day(client: TestClient) -> None:
    _save(_days_ago(3), "older", "Reply to the landlord")
    pill = "From " + (_today() - timedelta(days=3)).strftime("%d %b").lstrip("0")

    response = client.get("/brief")

    assert response.status_code == 200
    assert "Reply to the landlord" in response.text
    assert pill in response.text


def test_brief_has_no_pill_when_nothing_carries_over(client: TestClient) -> None:
    response = client.get("/brief")

    assert response.status_code == 200
    assert "Still open from the" not in response.text


def test_done_carried_item_leaves_todays_brief(client: TestClient) -> None:
    item_id = _save(_days_ago(1), "finish-me", "Finish the slides")

    assert client.post(f"/items/{item_id}/done").status_code == 200

    assert "Finish the slides" not in client.get("/brief").text
    with db.session() as conn:
        row = conn.execute("SELECT status, brief_date FROM items WHERE id = ?", (item_id,)).fetchone()
    # Carry-over never rewrites an item's day — it stays on its own brief.
    assert (row["status"], row["brief_date"]) == ("done", _days_ago(1))


def test_past_day_pages_show_only_that_day(client: TestClient) -> None:
    _save(_days_ago(4), "four", "Four days old")
    _save(_days_ago(2), "two", "Two days old")

    page = client.get(f"/history/{_days_ago(2)}")

    assert page.status_code == 200
    assert "Two days old" in page.text
    assert "Four days old" not in page.text


def test_lanes_partial_matches_the_brief(client: TestClient) -> None:
    _save(_days_ago(1), "partial", "Call the bank")

    today_partial = client.get("/brief/lanes-partial").json()
    past_partial = client.get(f"/brief/lanes-partial?date={_days_ago(1)}").json()

    assert "Call the bank" in today_partial["html"]
    assert today_partial["lane_counts"]["action_items"] >= 1
    assert "Still open from the" not in past_partial["html"]


def test_add_carried_over_skips_a_lane_that_no_longer_exists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = _today().isoformat()
    monkeypatch.setattr(
        queries, "list_carried_over",
        lambda *_: [{"lane": "retired_lane", "brief_date": _days_ago(1), "title": "x"}],
    )
    brief = {"brief_date": today, "lanes": {"action_items": []}}

    carryover.add_carried_over(brief, today)

    assert brief["lanes"] == {"action_items": []}
