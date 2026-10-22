"""User-defined rules around the LLM triage pass — e.g. "always put
anything from this sender in the Urgent lane", or "promotions never reach
the brief's work lanes".

Two moments, one rule list (first enabled match wins; the user's own
rules are checked before the built-in defaults):

- **Before** the LLM (`prefilter`): an item whose first matching rule
  sends it to Reading or Skip never reaches the model. Newsletters,
  promotions, job alerts and surveys are decided deterministically — no
  tokens spent, no "do it now" advice invented for an ad.
- **After** the LLM (`apply_rules`): a rule naming a work lane overrides
  the model's lane, keeping its summary. Predictable regardless of which
  provider (or none) ran, and effective on the very next pull.

Fields: title / sender (substring), category (a Gmail inbox category —
promotions, social, updates, forums, personal), bulk (mass mail: has a
List-Unsubscribe header and isn't in Gmail's Primary category).
"""

from command_center import queries, triage
from command_center.config import LANES
from command_center.sources import RawItem

FIELDS = ("title", "sender", "category", "bulk")
SKIP = "skip"  # a rule "lane" meaning: drop the item from the brief entirely
RULE_LANES = (*LANES, SKIP)
PREFILTER_LANES = ("reading", SKIP)  # decided before the LLM ever sees the item
GMAIL_CATEGORIES = ("promotions", "social", "updates", "forums", "personal")

# Seeded once, as ordinary rules the user can see, disable or delete in
# Settings → Triage rules. Promotions, social notifications, job alerts
# and paid surveys are hidden (they stay in Gmail) — the owner asked for
# Reading to hold articles only. Other bulk mail (newsletters) still goes
# to Reading, where reading.pick keeps the few worth reading.
HIDDEN_NOISE = (
    ("category", "promotions"),
    ("category", "social"),
    ("sender", "jobalerts-noreply@linkedin.com"),
    ("sender", "jobs-noreply@linkedin.com"),
    ("sender", "jobs-listings@linkedin.com"),
    ("sender", "userinterviews.com"),
    ("sender", "respondent.io"),
    ("sender", "surveymonkey"),
    ("sender", "typeform"),
    ("sender", "qualtrics"),
    ("sender", "prolific"),
    ("sender", "dscout"),
    ("sender", "usertesting"),
)
BUILTIN_RULES = (
    *((field, value, SKIP) for field, value in HIDDEN_NOISE),
    ("bulk", "yes", "reading"),
)


def _gmail_categories(raw: RawItem | None) -> set[str]:
    labels = (raw.metadata.get("labels") if raw else None) or []
    return {label[len("CATEGORY_"):].lower() for label in labels if label.startswith("CATEGORY_")}


def _matches(rule: dict, title: str, raw: RawItem | None) -> bool:
    field = rule["field"]
    if field == "title":
        return rule["match_value"].lower() in (title or "").lower()
    if field == "sender":
        sender = (raw.metadata.get("from") if raw else None) or ""
        return bool(sender) and rule["match_value"].lower() in sender.lower()
    if field == "category":
        return rule["match_value"].lower() in _gmail_categories(raw)
    if field == "bulk":
        if not raw or not raw.metadata.get("list_unsubscribe"):
            return False
        categories = _gmail_categories(raw)
        # No category labels at all (not Gmail's tabbed inbox) or Primary:
        # a list mail the user chose to see up front — leave it to the LLM.
        return bool(categories) and "personal" not in categories
    return False


def _first_match(rules: list[dict], title: str, raw: RawItem | None) -> dict | None:
    return next((rule for rule in rules if _matches(rule, title, raw)), None)


def prefilter(raw_items: list[RawItem]) -> tuple[list[RawItem], list[dict]]:
    """Splits raw items into (still for the LLM, already decided). Decided
    items whose rule says Reading come back as ready-made Reading-lane
    items; Skip ones are dropped. Everything else goes on to triage."""
    rules = queries.list_triage_rules(enabled_only=True)
    if not rules:
        return raw_items, []

    # Among the built-ins, hide rules win over keep rules: a LinkedIn job
    # alert is also bulk mail, and "hide job alerts" must beat "bulk mail
    # goes to Reading". The user's own rules still come first, so one of
    # theirs always beats a default. sorted() is stable.
    rules = sorted(rules, key=lambda rule: (rule["builtin"], rule["lane"] != SKIP))

    for_llm: list[RawItem] = []
    decided: list[dict] = []
    for raw in raw_items:
        rule = _first_match(rules, raw.title, raw)
        if rule is None or rule["lane"] not in PREFILTER_LANES:
            for_llm.append(raw)
        elif rule["lane"] == "reading":
            decided.append(triage.item_from_raw(raw, lane="reading", priority=3))
    return for_llm, decided


def apply_rules(triaged_items: list[dict], raw_items: list[RawItem]) -> list[dict]:
    """Mutates triaged_items in place (and returns it) — for each item,
    the first enabled matching rule that names a work lane overwrites the
    LLM's lane. Items no rule matches are left exactly as triage produced
    them. raw_items is only needed to resolve sender/category/bulk (they
    live in RawItem.metadata, not in triage's own output shape).
    """
    rules = queries.list_triage_rules(enabled_only=True)
    if not rules:
        return triaged_items

    raw_by_key = {(ri.source, ri.source_id): ri for ri in raw_items}
    for item in triaged_items:
        raw = raw_by_key.get((item.get("source"), item.get("source_id")))
        rule = _first_match(rules, item.get("title") or "", raw)
        if rule is not None and rule["lane"] in LANES:
            item["lane"] = rule["lane"]
    return triaged_items


def seed_builtin_rules() -> None:
    """Adds BUILTIN_RULES once per install (an app_settings flag records
    it), so deleting one in Settings sticks across restarts."""
    queries.seed_builtin_triage_rules(BUILTIN_RULES)
    # Installs seeded before the noise rules switched to hidden: flip the
    # saved ones too — once, and only those still on their default Reading.
    queries.hide_builtin_noise_rules(HIDDEN_NOISE)
