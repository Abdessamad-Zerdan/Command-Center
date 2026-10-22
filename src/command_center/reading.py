"""The Reading lane: a few articles worth the time, not every newsletter.

Rules already hide promotions, social notifications, job alerts and
surveys (triage_rules.BUILTIN_RULES). What's left in Reading is mostly
newsletters, where only judgment tells a real article ("90% of AI news
is useless for devs") from a sales mail in newsletter form ("Still deal
hunting?"). So each pull makes one small model call over the Reading
candidates' titles and senders, keeping at most MAX_PICKS with a
one-line reason each — and the brief shows at most MAX_SHOWN at a time
(cap_lane), so earlier days' picks can't pile up into a list nobody
reads.
"""

import logging
from datetime import date, datetime, timedelta

from command_center import queries, triage
from command_center.sources import RawItem
from command_center.triage import TriageProviderError

logger = logging.getLogger(__name__)

MAX_PICKS = 5   # kept per pull
MAX_SHOWN = 5   # on the brief at once, earlier days' picks included
MAX_AGE_DAYS = 3  # an unread pick older than this drops off the brief

SYSTEM_PROMPT = ""  # withheld from the public showcase - see README.md


def _system_prompt() -> str:
    # One output language for every card (the configured summary
    # language), like triage's own reasons.
    raise TriageProviderError("_system_prompt: withheld from the public showcase - see README.md")


def _ask_model(candidates: list[dict], raw_by_key: dict) -> list[dict]:
    raise TriageProviderError("_ask_model: withheld from the public showcase - see README.md")


def pick(items: list[dict], raw_items: list[RawItem]) -> tuple[list[dict], list[str]]:
    """Returns (items with Reading narrowed to the picks, degraded). Other
    lanes pass through untouched. If the model call fails, no candidate
    is kept and "reading" is marked degraded: unranked newsletters are
    exactly the noise Reading exists to keep out, so an empty, honestly
    labelled lane beats a list of whatever came first."""
    # Withheld: nothing is picked, and Reading shows as degraded.
    return [i for i in items if i.get("lane") != "reading"], ["reading"]


def cap_lane(lanes: dict, today: str) -> None:
    """In place: Reading shows at most MAX_SHOWN unread picks (priority 1
    or 2 — mail filed before the picker existed is priority 3) from today
    and the MAX_AGE_DAYS - 1 days before, best first (priority, then
    newest). Earlier days' picks carry a "From 21 Oct" label like any
    carried-over card. Everything else stays in the database, off the
    brief."""
    if "reading" not in lanes:
        return
    oldest = (datetime.fromisoformat(today) - timedelta(days=MAX_AGE_DAYS - 1)).date().isoformat()
    earlier = queries.list_recent_picked_reading(oldest, today)
    for item in earlier:
        item["carried_from"] = date.fromisoformat(item["brief_date"]).strftime("%d %b").lstrip("0")
    seen = {i.get("id") for i in lanes["reading"]}
    candidates = lanes["reading"] + [i for i in earlier if i["id"] not in seen]
    picked = [i for i in candidates
              if (i.get("brief_date") or today) >= oldest and (i.get("priority") or 3) <= 2]
    picked.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    picked.sort(key=lambda i: i.get("priority") or 3)
    lanes["reading"] = picked[:MAX_SHOWN]
