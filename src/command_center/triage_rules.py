"""User-defined rules layered on top of the LLM triage pass — e.g.
"always put anything from this sender in the Urgent lane". Applied as a
deterministic post-processing step over triage's already-produced
output, not baked into the triage prompt — that keeps them predictable
regardless of which provider (or none) triage actually used, and means
a rule change takes effect on the very next pull, not on some future
prompt-tuning pass.
"""

from command_center import queries
from command_center.sources import RawItem

FIELDS = ("title", "sender")


def apply_rules(triaged_items: list[dict], raw_items: list[RawItem]) -> list[dict]:
    """Mutates triaged_items in place (and returns it) — for each item,
    the first enabled rule (in creation order) whose match_value appears
    in the relevant field wins and overwrites that item's lane. Items no
    rule matches are left exactly as triage produced them. raw_items is
    only needed to resolve "sender" (Gmail's From header lives in
    RawItem.metadata, not in triage's own output shape); "title" reads
    straight off the triaged item.
    """
    rules = queries.list_triage_rules(enabled_only=True)
    if not rules:
        return triaged_items

    raw_by_key = {(ri.source, ri.source_id): ri for ri in raw_items}

    for item in triaged_items:
        raw = raw_by_key.get((item.get("source"), item.get("source_id")))
        for rule in rules:
            value = _field_value(rule["field"], item, raw)
            if value and rule["match_value"].lower() in value.lower():
                item["lane"] = rule["lane"]
                break

    return triaged_items


def _field_value(field: str, item: dict, raw: RawItem | None) -> str:
    if field == "title":
        return item.get("title") or ""
    if field == "sender":
        return (raw.metadata.get("from") if raw else None) or ""
    return ""
