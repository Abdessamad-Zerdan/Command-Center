from pathlib import Path

import pytest

from command_center import db, queries, triage_rules
from command_center.sources import RawItem


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _raw(source: str, source_id: str, sender: str = "") -> RawItem:
    return RawItem(
        source=source,
        source_id=source_id,
        title="Doesn't matter here",
        body="",
        metadata={"from": sender} if sender else {},
    )


def _triaged(source: str, source_id: str, title: str, lane: str = "action_items") -> dict:
    return {"source": source, "source_id": source_id, "title": title, "lane": lane}


def test_apply_rules_no_rules_is_a_no_op(isolated_db: None) -> None:
    items = [_triaged("gmail", "m1", "Server down")]
    result = triage_rules.apply_rules(items, [_raw("gmail", "m1")])
    assert result[0]["lane"] == "action_items"


def test_apply_rules_title_match_overrides_lane(isolated_db: None) -> None:
    queries.create_triage_rule("title", "server down", "urgent")
    items = [_triaged("gmail", "m1", "Server down in prod")]

    triage_rules.apply_rules(items, [_raw("gmail", "m1")])

    assert items[0]["lane"] == "urgent"


def test_apply_rules_title_match_is_case_insensitive(isolated_db: None) -> None:
    queries.create_triage_rule("title", "SERVER DOWN", "urgent")
    items = [_triaged("gmail", "m1", "server down in prod")]

    triage_rules.apply_rules(items, [_raw("gmail", "m1")])

    assert items[0]["lane"] == "urgent"


def test_apply_rules_sender_match_overrides_lane(isolated_db: None) -> None:
    queries.create_triage_rule("sender", "boss@example.com", "urgent")
    items = [_triaged("gmail", "m1", "Quick question")]

    triage_rules.apply_rules(items, [_raw("gmail", "m1", sender="boss@example.com")])

    assert items[0]["lane"] == "urgent"


def test_apply_rules_sender_rule_does_not_match_on_title(isolated_db: None) -> None:
    queries.create_triage_rule("sender", "boss@example.com", "urgent")
    items = [_triaged("gmail", "m1", "Email from boss@example.com in the subject")]

    triage_rules.apply_rules(items, [_raw("gmail", "m1", sender="someone-else@example.com")])

    assert items[0]["lane"] == "action_items"  # unchanged — matched field was title text, not sender


def test_apply_rules_no_match_leaves_lane_unchanged(isolated_db: None) -> None:
    queries.create_triage_rule("title", "nonexistent phrase", "urgent")
    items = [_triaged("gmail", "m1", "Server down")]

    triage_rules.apply_rules(items, [_raw("gmail", "m1")])

    assert items[0]["lane"] == "action_items"


def test_apply_rules_disabled_rule_is_ignored(isolated_db: None) -> None:
    rule_id = queries.create_triage_rule("title", "server down", "urgent")
    queries.set_triage_rule_enabled(rule_id, False)
    items = [_triaged("gmail", "m1", "Server down")]

    triage_rules.apply_rules(items, [_raw("gmail", "m1")])

    assert items[0]["lane"] == "action_items"


def test_apply_rules_first_matching_rule_wins(isolated_db: None) -> None:
    queries.create_triage_rule("title", "server", "urgent")
    queries.create_triage_rule("title", "down", "tasks_due")
    items = [_triaged("gmail", "m1", "Server down")]

    triage_rules.apply_rules(items, [_raw("gmail", "m1")])

    assert items[0]["lane"] == "urgent"  # the rule created first


def test_apply_rules_item_with_no_matching_raw_item_only_gets_title_rules(
    isolated_db: None,
) -> None:
    # A reading-lane item from Medium has no raw_items counterpart at
    # all — sender rules can't match it (no metadata to read), but title
    # rules still apply since the title comes straight off the item.
    queries.create_triage_rule("sender", "boss@example.com", "urgent")
    queries.create_triage_rule("title", "must read", "urgent")
    items = [_triaged("medium", "article-1", "A must read article")]

    triage_rules.apply_rules(items, [])

    assert items[0]["lane"] == "urgent"
