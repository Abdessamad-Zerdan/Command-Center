"""Still-open work from earlier days, shown on today's brief.

An item lives on the day it arrived (items.brief_date), so without this
a task that came in on Monday and is still open vanishes from /brief on
Tuesday — today's page only showed today's arrivals. Carried-over items
keep their own brief_date (nothing is moved in the database); they're
just listed in their lane after today's own items, with a "From 12 Oct"
pill, so marking one done/dismissed works exactly as on its own day.
"""

from datetime import date
from typing import Any

from command_center import queries

# Older than this and it's history, not today's work — the History page
# (and its "Bring to today") still reaches it.
CARRY_OVER_DAYS = 14


def _day_label(brief_date: str) -> str:
    return date.fromisoformat(brief_date).strftime("%d %b").lstrip("0")


def add_carried_over(brief: dict[str, Any], today: str) -> None:
    """Appends earlier days' pending items to `brief`'s lanes, in place.
    Only for today's own brief — a past day's page shows just that day."""
    if brief["brief_date"] != today:
        return
    for item in queries.list_carried_over(today, CARRY_OVER_DAYS):
        if item["lane"] not in brief["lanes"]:
            continue  # a lane that no longer exists — nowhere to show it
        item["carried_from"] = _day_label(item["brief_date"])
        brief["lanes"][item["lane"]].append(item)
