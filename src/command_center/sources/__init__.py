"""Common interface every ingestion source implements."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class RawItem:
    source: str  # "gmail" | "calendar"
    source_id: str
    title: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Source(Protocol):
    def fetch(self) -> list[RawItem]: ...
