"""The deterministic filter in front of the LLM (triage_rules.prefilter):
promotions, social, job alerts and surveys are hidden and other bulk mail
goes to Reading, without the model ever seeing them; the user's own rules
win over the built-in defaults."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, db, pipeline, queries, triage, triage_rules
from command_center.assistant import ingest as assistant_ingest
from command_center.setup_wizard import status as setup_status
from command_center.sources import RawItem
from command_center.sources.gmail import GmailSource


@pytest.fixture()
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    triage_rules.seed_builtin_rules()


def _mail(source_id: str, title: str = "Hello", sender: str = "Someone <a@b.com>", labels=(), bulk: bool = False) -> RawItem:
    return RawItem(
        source="gmail", source_id=source_id, title=title, body="",
        metadata={"from": sender, "labels": list(labels), "list_unsubscribe": bulk, "deep_link": "https://mail/x"},
    )


def _split(items):
    for_llm, decided = triage_rules.prefilter(items)
    return [r.source_id for r in for_llm], {d["source_id"]: d["lane"] for d in decided}


def test_noise_is_decided_before_the_llm(seeded: None) -> None:
    items = [
        _mail("promo", "NVIDIA GTC: add the keynote to your calendar", labels=["INBOX", "CATEGORY_PROMOTIONS"]),
        _mail("social", "Someone viewed your profile", labels=["CATEGORY_SOCIAL"]),
        _mail("jobs", "Amazon Business Analyst, Operations", sender="LinkedIn <jobalerts-noreply@linkedin.com>"),
        _mail("survey", "$5 for 5 min: share your thoughts", sender="User Interviews <hello@userinterviews.com>"),
        _mail("newsletter", "This week in AI", labels=["CATEGORY_UPDATES"], bulk=True),
        _mail("boss", "Can you review the contract today?", labels=["INBOX", "CATEGORY_PERSONAL"]),
        _mail("list-in-primary", "Agenthon: submission closes Friday", labels=["CATEGORY_PERSONAL"], bulk=True),
    ]

    for_llm, decided = _split(items)

    assert for_llm == ["boss", "list-in-primary"]
    # Promotions, social, job alerts and surveys are hidden; a newsletter
    # still reaches Reading (where reading.pick keeps the worthwhile ones).
    assert decided == {"newsletter": "reading"}


def test_a_job_alert_sent_as_bulk_mail_is_still_hidden(seeded: None) -> None:
    # It matches both "bulk mail -> Reading" and "job alerts -> hide";
    # hiding must win, whatever order the built-ins were saved in.
    item = _mail("jobs", "New jobs for you", sender="LinkedIn <jobalerts-noreply@linkedin.com>",
                 labels=["CATEGORY_UPDATES"], bulk=True)
    assert _split([item]) == ([], {})


def test_a_users_own_rule_beats_a_builtin_hide_rule(seeded: None) -> None:
    queries.create_triage_rule("sender", "linkedin.com", "reading")
    item = _mail("jobs", "New jobs for you", sender="LinkedIn <jobalerts-noreply@linkedin.com>")
    assert _split([item]) == ([], {"jobs": "reading"})


def test_decided_items_have_no_invented_advice(seeded: None) -> None:
    _, decided = triage_rules.prefilter([_mail("news", labels=["CATEGORY_UPDATES"], bulk=True)])
    [item] = decided
    assert (item["why_it_matters"], item["suggested_next_step"], item["priority"]) == ("", "", 3)
    assert item["deep_link"] == "https://mail/x"


def test_a_users_own_rule_wins_over_the_builtins(seeded: None) -> None:
    queries.create_triage_rule("sender", "agenthon", "tasks_due")
    item = _mail("agenthon", "Final call", sender="news@agenthon.net", labels=["CATEGORY_UPDATES"], bulk=True)

    for_llm, decided = _split([item])
    assert (for_llm, decided) == (["agenthon"], {})  # not swallowed by the bulk default

    triaged = [triage.item_from_raw(item, lane="reading")]
    triage_rules.apply_rules(triaged, [item])
    assert triaged[0]["lane"] == "tasks_due"


def test_skip_drops_an_item_entirely(seeded: None) -> None:
    queries.create_triage_rule("title", "webinar", "skip")
    for_llm, decided = _split([_mail("w", "Free webinar tomorrow")])
    assert (for_llm, decided) == ([], {})


def test_disabled_builtin_lets_the_item_through(seeded: None) -> None:
    promo_rule = next(r for r in queries.list_triage_rules() if r["match_value"] == "promotions")
    queries.set_triage_rule_enabled(promo_rule["id"], False)
    for_llm, _ = _split([_mail("promo", labels=["CATEGORY_PROMOTIONS"])])
    assert for_llm == ["promo"]


def test_builtins_are_seeded_once_and_deletions_stick(seeded: None) -> None:
    rules = queries.list_triage_rules()
    assert len(rules) == len(triage_rules.BUILTIN_RULES) and all(r["builtin"] for r in rules)
    queries.delete_triage_rule(rules[0]["id"])
    triage_rules.seed_builtin_rules()
    assert len(queries.list_triage_rules()) == len(triage_rules.BUILTIN_RULES) - 1


def test_pipeline_only_sends_what_is_left_to_the_llm(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def fake_run(raw_items, model=None):
        sent.extend(r.source_id for r in raw_items)
        return [triage.item_from_raw(r, lane="action_items") for r in raw_items]

    monkeypatch.setattr(triage, "run", fake_run)
    items, degraded = pipeline._triage([_mail("promo", labels=["CATEGORY_PROMOTIONS"]), _mail("real")], "test")

    assert sent == ["real"] and degraded == []
    assert {i["source_id"]: i["lane"] for i in items} == {"real": "action_items"}  # promo hidden


def test_a_broken_prefilter_falls_back_to_the_llm(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_):
        raise RuntimeError("rules bug")

    sent: list[str] = []
    monkeypatch.setattr(triage_rules, "prefilter", boom)
    monkeypatch.setattr(
        triage, "run", lambda raw, model=None: sent.extend(r.source_id for r in raw) or [triage.item_from_raw(r, lane="urgent") for r in raw]
    )
    items, degraded = pipeline._triage([_mail("news", labels=["CATEGORY_UPDATES"], bulk=True)], "test")
    # Nothing lost: the LLM still triaged it (and the post-LLM rule pass,
    # which didn't break, still moved the newsletter to Reading).
    assert sent == ["news"] and degraded == []
    assert [i["lane"] for i in items] == ["reading"]


def test_gmail_keeps_category_labels_and_list_unsubscribe() -> None:
    message = {
        "id": "m1", "internalDate": "1791800000000", "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"],
        "payload": {"headers": [
            {"name": "Subject", "value": "Sale"}, {"name": "From", "value": "shop@x.com"},
            {"name": "List-Unsubscribe", "value": "<mailto:unsub@x.com>"},
        ], "mimeType": "text/plain", "body": {}},
    }

    class _Call:
        def __init__(self, result):
            self._result = result

        def execute(self):
            return self._result

    class _Messages:
        def list(self, **_):
            return _Call({"messages": [{"id": "m1"}]})

        def get(self, **_):
            return _Call(message)

    class _Users:
        def messages(self):
            return _Messages()

    class _Service:
        def users(self):
            return _Users()

    source = GmailSource.__new__(GmailSource)
    source._service = _Service()
    [item] = source.fetch()
    assert item.metadata["labels"] == ["INBOX", "CATEGORY_PROMOTIONS"]
    assert item.metadata["list_unsubscribe"] is True


# --- Settings route -------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def test_settings_accepts_category_bulk_and_skip_rules(client: TestClient) -> None:
    ok_category = client.post("/settings/triage-rules", json={"field": "category", "match_value": "Forums", "lane": "skip"})
    ok_bulk = client.post("/settings/triage-rules", json={"field": "bulk", "match_value": "", "lane": "reading"})
    bad_category = client.post("/settings/triage-rules", json={"field": "category", "match_value": "spam", "lane": "reading"})

    assert (ok_category.status_code, ok_bulk.status_code, bad_category.status_code) == (200, 200, 400)
    user_rules = [r for r in queries.list_triage_rules() if not r["builtin"]]
    assert [(r["field"], r["match_value"], r["lane"]) for r in user_rules] == [
        ("category", "forums", "skip"), ("bulk", "yes", "reading"),
    ]
    page = client.get("/settings/triage-rules").text
    assert "Built-in" in page and "Skip (drop it)" in page
    # Regression: the rules JSON (full of double quotes) used to sit inside
    # the double-quoted x-data attribute — fine with zero rules, a broken
    # page as soon as any existed. It now lives in a <script> block.
    assert "rules: window.TRIAGE_RULES" in page
    x_data = page.split('x-data="', 1)[1].split('"', 1)[0]
    assert "match_value" not in x_data or "rule.match_value" in x_data


# --- one-time switch of an older install's saved noise rules ---------------------------


def _set_lane(rule_id: int, lane: str) -> None:
    with db.session() as conn:
        conn.execute("UPDATE triage_rules SET lane = ? WHERE id = ?", (lane, rule_id))


def test_an_older_installs_saved_noise_rules_switch_to_hidden_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    # An install seeded before the change: every built-in went to Reading.
    queries.seed_builtin_triage_rules((
        ("category", "promotions", "reading"),
        ("category", "social", "reading"),
        ("bulk", "yes", "reading"),
        ("sender", "jobalerts-noreply@linkedin.com", "reading"),
    ))
    social = next(r for r in queries.list_triage_rules() if r["match_value"] == "social")
    _set_lane(social["id"], "urgent")  # the user changed this one themselves

    triage_rules.seed_builtin_rules()

    lanes = {r["match_value"]: r["lane"] for r in queries.list_triage_rules()}
    assert lanes == {
        "promotions": "skip",
        "social": "urgent",      # the user's own choice is kept
        "yes": "reading",        # newsletters still go to Reading
        "jobalerts-noreply@linkedin.com": "skip",
    }

    # Once only: a rule the user switches back to Reading afterwards stays.
    promo = next(r for r in queries.list_triage_rules() if r["match_value"] == "promotions")
    _set_lane(promo["id"], "reading")
    triage_rules.seed_builtin_rules()
    assert next(r for r in queries.list_triage_rules() if r["id"] == promo["id"])["lane"] == "reading"
