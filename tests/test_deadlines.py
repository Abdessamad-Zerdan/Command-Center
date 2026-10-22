"""Date awareness in triage: deadlines are parsed with their own time zone
(AoE included), and one that has already passed is never presented as
"do it now" — whatever the LLM said."""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, deadlines, queries, triage
from command_center.assistant import ingest as assistant_ingest
from command_center.config import TZ
from command_center.setup_wizard import status as setup_status

ISTANBUL = ZoneInfo("Europe/Istanbul")
# The Agenthon case from the review: closed 12 Oct 23:59 AoE, read on 13 Oct.
NOW = datetime(2026, 10, 13, 14, 0, tzinfo=ISTANBUL)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("12 Oct 23:59 AoE", datetime(2026, 10, 12, 23, 59, tzinfo=deadlines._ZONES["aoe"])),
        ("2026-10-12 23:59 AoE", datetime(2026, 10, 12, 23, 59, tzinfo=deadlines._ZONES["aoe"])),
        ("2026-10-12T23:59:00-12:00", datetime(2026, 10, 12, 23, 59, tzinfo=deadlines._ZONES["aoe"])),
        ("Oct 20, 5pm PT", datetime(2026, 10, 20, 17, 0, tzinfo=ZoneInfo("America/Los_Angeles"))),
        ("26 Oct 14:59 Istanbul time", datetime(2026, 10, 26, 14, 59, tzinfo=ISTANBUL)),
        ("20 Oct 2026 12:00 UTC+3", datetime(2026, 10, 20, 9, 0, tzinfo=ZoneInfo("UTC"))),
        ("2026-11-01", datetime(2026, 11, 1, 23, 59, 59, tzinfo=ISTANBUL)),  # date only → end of day, own zone
    ],
)
def test_parse_reads_dates_and_time_zones(text: str, expected: datetime) -> None:
    assert deadlines.parse(text, ISTANBUL, NOW.date()) == expected


@pytest.mark.parametrize("text", [None, "", "  ", "next week", "asap"])
def test_parse_gives_up_rather_than_guessing(text) -> None:
    assert deadlines.parse(text, ISTANBUL, NOW.date()) is None


def test_aoe_is_not_past_until_it_is_past_everywhere() -> None:
    # 12 Oct 23:59 AoE = 13 Oct 14:59 in Istanbul.
    deadline = "12 Oct 23:59 AoE"
    assert deadlines.apply([{"lane": "tasks_due", "deadline": deadline}], NOW.replace(hour=14, minute=58))[0]["lane"] == "tasks_due"
    assert deadlines.apply([{"lane": "tasks_due", "deadline": deadline}], NOW.replace(hour=15, minute=0))[0]["lane"] == "action_items"


def test_a_missed_deadline_is_never_do_it_now() -> None:
    [item] = deadlines.apply(
        [{"lane": "tasks_due", "priority": 1, "deadline": "2026-10-12 23:59 AoE",
          "suggested_next_step": "Upload your submission immediately."}],
        NOW + timedelta(hours=2),
    )
    assert item["lane"] == "action_items" and item["priority"] == 3
    assert "Deadline passed" in item["suggested_next_step"]
    assert "immediately" not in item["suggested_next_step"]
    assert item["deadline"] == "2026-10-12T23:59:00-12:00"


def test_future_deadlines_and_no_deadline_are_left_alone() -> None:
    items = deadlines.apply(
        [{"lane": "urgent", "priority": 1, "deadline": "26 Oct 14:59 Istanbul time", "suggested_next_step": "Pick a track"},
         {"lane": "urgent", "priority": 1, "deadline": None, "suggested_next_step": "Reply"}],
        NOW,
    )
    assert [(i["lane"], i["priority"], i["suggested_next_step"]) for i in items] == [
        ("urgent", 1, "Pick a track"), ("urgent", 1, "Reply"),
    ]


# --- the card -------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def test_cards_show_passed_and_upcoming_deadlines(client: TestClient) -> None:
    today = datetime.now(TZ).date().isoformat()
    past = (datetime.now(TZ) - timedelta(days=1)).isoformat()
    future = (datetime.now(TZ) + timedelta(days=5)).isoformat()
    base = {"why_it_matters": "", "suggested_next_step": "", "priority": 2, "deep_link": "", "due_date": None}
    queries.save_triage_results(today, [
        {**base, "lane": "action_items", "source": "gmail", "source_id": "past", "title": "Closed thing", "deadline": past},
        {**base, "lane": "action_items", "source": "gmail", "source_id": "soon", "title": "Open thing", "deadline": future},
    ], [], [], ["gmail"])

    html = client.get("/brief").text

    assert "Deadline passed ·" in html
    assert "Deadline ·" in html
