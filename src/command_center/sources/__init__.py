"""Common interface every ingestion source implements."""

from dataclasses import dataclass, field
from typing import Any, Protocol


def to_local_iso(value: str | int | None) -> str | None:
    """A source's own timestamp — Gmail's internalDate (epoch ms) or a
    Google RFC3339 string — as an ISO string in the app's timezone, for
    items.source_time. None if missing or unparseable."""
    from datetime import datetime, timezone

    from command_center.config import TZ

    if value in (None, ""):
        return None
    try:
        if isinstance(value, int) or str(value).isdigit():
            when = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        else:
            when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    return when.astimezone(TZ).isoformat(timespec="seconds")


@dataclass
class RawItem:
    source: str  # "gmail" | "calendar"
    source_id: str
    title: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Source(Protocol):
    def fetch(self) -> list[RawItem]: ...
