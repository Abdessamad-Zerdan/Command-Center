"""The small date-and-time labels on cards ("Received 13 Oct, 14:05").

One formatter for every page so a mail, a task, a competition and a Map
card all read the same way. Times are shown in the app's timezone; the
year only appears when it isn't this year, and the full date-and-time
(with seconds) goes in the label's hover title.
"""

from datetime import datetime
from typing import Any

from fastapi.templating import Jinja2Templates

from command_center import deadlines
from command_center.config import TZ

# What the time on an item means, by where the item came from.
_ITEM_LABELS = {"gmail": "Received", "google_tasks": "Updated"}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=TZ)
    return when.astimezone(TZ)


def stamp(value: str | None) -> str:
    """'13 Oct, 14:05' — or '13 Oct 2025, 14:05' outside this year."""
    when = _parse(value)
    if when is None:
        return ""
    same_year = when.year == datetime.now(TZ).year
    return when.strftime("%d %b, %H:%M" if same_year else "%d %b %Y, %H:%M").lstrip("0")


def stamp_full(value: str | None) -> str:
    when = _parse(value)
    return when.strftime("%A %d %B %Y, %H:%M:%S") if when else ""


def item_stamp(item: dict[str, Any]) -> dict[str, str] | None:
    """{label, iso, text, full} for a task card: when an email arrived or a
    Google Task last changed, otherwise when the item was added here."""
    source_time = item.get("source_time")
    if source_time and item.get("source") in _ITEM_LABELS:
        label, value = _ITEM_LABELS[item["source"]], source_time
    else:
        label, value = "Added", item.get("created_at")
    if not _parse(value):
        return None
    return {"label": label, "iso": value, "text": stamp(value), "full": stamp_full(value)}


def deadline_label(item: dict[str, Any]) -> dict[str, Any] | None:
    """{text, full, missed} for an item's own deadline (see deadlines.py),
    shown in its time as the user lives it; None if it has none."""
    when = _parse(item.get("deadline"))
    if when is None:
        return None
    return {
        "text": stamp(item["deadline"]),
        "full": stamp_full(item["deadline"]),
        "missed": deadlines.is_missed(item["deadline"], datetime.now(TZ)),
    }


def install(templates: Jinja2Templates) -> None:
    templates.env.filters["stamp"] = stamp
    templates.env.filters["stamp_full"] = stamp_full
    templates.env.globals["item_stamp"] = item_stamp
    templates.env.globals["deadline_label"] = deadline_label
