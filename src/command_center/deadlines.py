"""Deadlines in triaged mail: parsing them (time zones included) and
making sure a deadline that has already passed is never presented as
"do it now".

The LLM reports a deadline it finds as text ("2026-10-12 23:59 AoE");
everything after that is deterministic code, not model judgement:
`parse` turns the text into an aware datetime, and `apply` marks items
whose deadline is past as missed — out of Urgent / Tasks due, priority
lowered, next step rewritten.
"""

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

# Time-zone words people actually put on deadlines. AoE ("Anywhere on
# Earth") is UTC-12: the deadline has passed only once it's passed
# everywhere. Daylight-saving names (PDT, CEST…) are fixed offsets; the
# bare region names (PT, ET, CET) follow the real zone's DST.
_ZONES: dict[str, tzinfo] = {
    "aoe": timezone(timedelta(hours=-12)),
    "anywhere on earth": timezone(timedelta(hours=-12)),
    "utc": timezone.utc, "gmt": timezone.utc, "z": timezone.utc,
    "pst": timezone(timedelta(hours=-8)), "pdt": timezone(timedelta(hours=-7)),
    "pt": ZoneInfo("America/Los_Angeles"), "pacific": ZoneInfo("America/Los_Angeles"),
    "est": timezone(timedelta(hours=-5)), "edt": timezone(timedelta(hours=-4)),
    "et": ZoneInfo("America/New_York"), "eastern": ZoneInfo("America/New_York"),
    "cet": ZoneInfo("Europe/Berlin"), "cest": timezone(timedelta(hours=2)),
    "trt": ZoneInfo("Europe/Istanbul"), "istanbul": ZoneInfo("Europe/Istanbul"),
    "istanbul time": ZoneInfo("Europe/Istanbul"), "turkey time": ZoneInfo("Europe/Istanbul"),
}
_OFFSET_RE = re.compile(r"\b(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\b", re.IGNORECASE)
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\s*(am|pm)?\b", re.IGNORECASE)
_HOUR_ONLY_RE = re.compile(r"\b(\d{1,2})\s*(am|pm)\b", re.IGNORECASE)  # "5pm", "11 am"
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})\s+([a-z]{3})[a-z]*\.?(?:\s+(\d{4}))?\b", re.IGNORECASE)
_MONTH_DAY_RE = re.compile(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?(?:\s+(\d{4}))?\b", re.IGNORECASE)


def _zone(text: str, default: tzinfo) -> tzinfo:
    offset = _OFFSET_RE.search(text)
    if offset:
        sign = 1 if offset.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(offset.group(2)), minutes=int(offset.group(3) or 0)))
    lowered = text.lower()
    # Longest names first, so "istanbul time" wins over a stray "t".
    for name in sorted(_ZONES, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lowered):
            return _ZONES[name]
    return default


def _date(text: str, today: date) -> date | None:
    iso = _ISO_DATE_RE.search(text)
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    for pattern, day_group, month_group in ((_DAY_MONTH_RE, 1, 2), (_MONTH_DAY_RE, 2, 1)):
        for match in pattern.finditer(text):
            month = _MONTHS.get(match.group(month_group)[:3].lower())
            if not month:
                continue
            year = int(match.group(3)) if match.group(3) else today.year
            try:
                return date(year, month, int(match.group(day_group)))
            except ValueError:
                continue
    return None


def parse(text: str | None, default_tz: tzinfo, today: date | None = None) -> datetime | None:
    """A deadline as written ("12 Oct 23:59 AoE", "2026-10-12T23:59:00-12:00",
    "Oct 20, 5pm PT", "2026-11-01") → an aware datetime, or None if no date
    can be found. A date with no time means the end of that day; no zone
    means `default_tz` (the user's own)."""
    if not text or not str(text).strip():
        return None
    text = str(text).strip()
    parsed = None
    if not _DATE_ONLY_RE.match(text):  # a bare date means the end of that day, not midnight
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        return parsed

    zone = _zone(text, default_tz)
    if parsed is not None:  # ISO without an offset
        return parsed.replace(tzinfo=zone)
    day = _date(text, today or datetime.now(default_tz).date())
    if day is None:
        return None
    clock = _TIME_RE.search(text)
    hour_only = None if clock else _HOUR_ONLY_RE.search(text)
    if clock or hour_only:
        if clock:
            hour, minute, meridiem = int(clock.group(1)), int(clock.group(2)), (clock.group(3) or "").lower()
        else:
            hour, minute, meridiem = int(hour_only.group(1)), 0, hour_only.group(2).lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        at = time(min(hour, 23), min(minute, 59))
    else:
        at = time(23, 59, 59)
    return datetime.combine(day, at, tzinfo=zone)


_DUE_NOW_LANES = ("urgent", "tasks_due")


def apply(items: list[dict], now: datetime) -> list[dict]:
    """Mutates triaged items in place: normalises each `deadline` to ISO,
    and an item whose deadline is already past becomes "missed" — moved
    out of Urgent / Tasks due into Action items, priority 3, next step
    rewritten so it never says to act now. Items without a parseable
    deadline are left as they are (deadline None)."""
    for item in items:
        when = parse(item.get("deadline"), now.tzinfo, now.date())
        item["deadline"] = when.isoformat() if when else None
        if when is None or when > now:
            continue
        local = when.astimezone(now.tzinfo)
        item["priority"] = 3
        if item.get("lane") in _DUE_NOW_LANES:
            item["lane"] = "action_items"
        item["suggested_next_step"] = (
            f"Deadline passed ({local.strftime('%d %b, %H:%M')}) — nothing to submit now; "
            "check whether a late entry or the next round is possible."
        )
    return items


def is_missed(deadline_iso: str | None, now: datetime) -> bool:
    if not deadline_iso:
        return False
    try:
        return datetime.fromisoformat(deadline_iso) <= now
    except ValueError:
        return False
