"""Proactive nudges: cheap, no-network signals surfaced as a dismissible
banner on /brief. Computed fresh on every dashboard load — nothing here
is persisted or scheduled, so a nudge simply stops showing once its
underlying condition clears (the item is touched/completed, or a pull
lands). Dismissing a nudge in the UI is per-page-view only, not saved.
"""

from datetime import datetime, timedelta

from command_center import queries
from command_center.config import TZ

_STALE_URGENT_DAYS = 2
# Local pulls typically land well before noon — nudging earlier would
# just flag a normal morning, not a stuck scheduler.
_NO_PULL_HOUR = 12


def compute_nudges(brief_date: str, now: datetime | None = None) -> list[dict]:
    # Resolved here, not a `now: datetime = datetime.now(TZ)` default
    # argument — default-argument expressions bind once at import time,
    # which would freeze "now" at process start. `now` is also the test
    # seam: callers pass a fixed value instead of depending on wall clock.
    now = now or datetime.now(TZ)
    nudges = []

    stale = queries.list_stale_pending_items(
        "urgent", (now - timedelta(days=_STALE_URGENT_DAYS)).isoformat()
    )
    if stale:
        nudges.append(
            {
                "id": "stale_urgent",
                "message": f"{_count(stale)} sat in Urgent for {_STALE_URGENT_DAYS}+ days untouched: "
                + _examples(stale),
                "targets": [{"id": item["id"]} for item in stale],
            }
        )

    overdue = queries.list_overdue_tasks(brief_date)
    if overdue:
        nudges.append(
            {
                "id": "overdue_tasks",
                "message": f"{_count(overdue)} overdue: " + _examples(overdue),
                "targets": [{"id": item["id"]} for item in overdue],
            }
        )

    enabled_sources = [cfg for cfg in queries.list_source_configs() if cfg["enabled"]]
    if (
        now.hour >= _NO_PULL_HOUR
        and enabled_sources  # nothing enabled means no pull was ever expected
        and not any((cfg["last_pulled_at"] or "")[:10] == brief_date for cfg in enabled_sources)
    ):
        nudges.append(
            {
                "id": "no_pull_today",
                "message": "No sources have pulled today yet — check Settings if this seems stuck.",
            }
        )

    return nudges


def _count(items: list[dict]) -> str:
    n = len(items)
    return f"{n} item" if n == 1 else f"{n} items"


def _examples(items: list[dict], limit: int = 2) -> str:
    titles = ", ".join(f'"{item["title"]}"' for item in items[:limit])
    if len(items) > limit:
        titles += f", +{len(items) - limit} more"
    return titles
