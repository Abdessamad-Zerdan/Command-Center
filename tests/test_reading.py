"""reading.py: one model call keeps at most a few worthwhile articles from
the Reading candidates, and the brief shows at most MAX_SHOWN recent ones."""

import json
from pathlib import Path

import pytest

from command_center import db, queries, reading, triage
from command_center.sources import RawItem

_REAL_ASK_MODEL = reading._ask_model  # conftest stubs it for every other test


@pytest.fixture(autouse=True)
def _real_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reading, "_ask_model", _REAL_ASK_MODEL)


def _item(source_id: str, title: str, lane: str = "reading", priority: int = 3) -> dict:
    return {"source": "gmail", "source_id": source_id, "title": title, "lane": lane,
            "why_it_matters": "", "suggested_next_step": "", "priority": priority}


def _raw(source_id: str, sender: str) -> RawItem:
    return RawItem(source="gmail", source_id=source_id, title="", body="", metadata={"from": sender})


@pytest.fixture()
def model(monkeypatch: pytest.MonkeyPatch):
    """Scripted picker reply; records what the model was sent."""
    sent: list[dict] = []
    reply = {"picks": []}
    monkeypatch.setattr(reading.queries, "get_summary_language", lambda: "English")

    def fake(messages, max_tokens=1024):
        sent.append(json.loads(messages[-1]["content"]))
        return json.dumps(reply) if isinstance(reply, dict) else reply

    monkeypatch.setattr(triage, "run_groq_chat", fake)
    return sent, reply


@pytest.mark.parametrize("reply", ["not json", json.dumps({"picks": "nope"})])
def test_a_bad_reply_keeps_no_newsletters_and_says_so(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    monkeypatch.setattr(triage, "run_groq_chat", lambda messages, max_tokens=1024: reply)
    items = [_item(f"a{n}", f"Article {n}") for n in range(12)]

    kept, degraded = reading.pick(items, [])

    assert (kept, degraded) == ([], ["reading"])


def test_a_provider_outage_keeps_no_newsletters_but_the_rest_of_the_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(messages, max_tokens=1024):
        raise triage.TriageProviderError("Groq model returned an empty response.")

    monkeypatch.setattr(triage, "run_groq_chat", down)
    items = [_item(f"a{n}", f"Article {n}") for n in range(12)] + [_item("boss", "x", lane="urgent")]

    kept, degraded = reading.pick(items, [])

    assert degraded == ["reading"]
    assert [i["source_id"] for i in kept] == ["boss"]


# --- cap_lane: what the brief shows -----------------------------------------------------


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _shown(source_id: str, brief_date: str, priority: int = 2, created_at: str = "") -> dict:
    return {"source_id": source_id, "brief_date": brief_date, "priority": priority,
            "created_at": created_at or f"{brief_date}T08:00:00+03:00"}


def test_cap_lane_shows_at_most_max_shown_best_first(isolated_db: None) -> None:
    lanes = {"reading": [_shown(f"r{n}", "2026-10-22", priority=2, created_at=f"2026-10-22T0{n}:00") for n in range(8)]
             + [_shown("top", "2026-10-21", priority=1)],
             "urgent": [_shown("u", "2026-10-01")]}

    reading.cap_lane(lanes, "2026-10-22")

    ids = [i["source_id"] for i in lanes["reading"]]
    assert len(ids) == reading.MAX_SHOWN
    assert ids[0] == "top"                          # priority first
    assert ids[1:] == ["r7", "r6", "r5", "r4"]      # then newest
    assert [i["source_id"] for i in lanes["urgent"]] == ["u"]  # other lanes untouched


def test_cap_lane_drops_picks_older_than_max_age(isolated_db: None) -> None:
    lanes = {"reading": [_shown("today", "2026-10-22"), _shown("two-days", "2026-10-20"),
                         _shown("old", "2026-10-19")]}

    reading.cap_lane(lanes, "2026-10-22")

    assert [i["source_id"] for i in lanes["reading"]] == ["today", "two-days"]


def test_cap_lane_with_no_reading_lane_is_a_no_op() -> None:
    lanes = {"urgent": []}
    reading.cap_lane(lanes, "2026-10-22")
    assert lanes == {"urgent": []}


def test_cap_lane_hides_unpicked_mail(isolated_db: None) -> None:
    # Priority 3 = filed by a rule without the picker (e.g. saved before
    # it existed): never shown, even with room to spare.
    lanes = {"reading": [_shown("picked", "2026-10-22", priority=2), _shown("junk", "2026-10-22", priority=3)]}

    reading.cap_lane(lanes, "2026-10-22")

    assert [i["source_id"] for i in lanes["reading"]] == ["picked"]


def _saved_reading(title: str, brief_date: str, priority: int) -> int:
    item_id = queries.create_manual_item(brief_date, "reading", title)
    with db.session() as conn:
        conn.execute("UPDATE items SET priority = ? WHERE id = ?", (priority, item_id))
    return item_id


def test_cap_lane_carries_earlier_days_picks_but_not_their_junk(isolated_db: None) -> None:
    pick = _saved_reading("Gemini 4 Argon, inside look", "2026-10-21", 1)
    _saved_reading("Still deal hunting?", "2026-10-21", 3)
    _saved_reading("Too old pick", "2026-10-19", 1)
    lanes = {"reading": [_shown("today", "2026-10-22", priority=2)]}

    reading.cap_lane(lanes, "2026-10-22")

    assert [i.get("id", i["source_id"]) for i in lanes["reading"]] == [pick, "today"]
    assert lanes["reading"][0]["carried_from"] == "21 Oct"
