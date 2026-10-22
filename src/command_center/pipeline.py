"""Orchestrates ingestion sources + triage into a saved brief. Each source
call is isolated: one failing degrades that lane, never takes down the
whole run — matches "never fail silently, never fail wholesale."
"""

import argparse
import logging
from datetime import datetime

from command_center import calendar_sync, prioritize, queries, reading, triage, triage_rules
from command_center.auth import get_google_credentials
from command_center.config import TZ
from command_center.sources import RawItem
from command_center.sources import medium
from command_center.sources.calendar import CalendarSource
from command_center.sources.gmail import GmailSource
from command_center.sources.tasks import TasksSource

logger = logging.getLogger(__name__)

# source_config row / "Pull now" / scheduler identifiers — distinct from
# RawItem.source ("google_tasks" etc), which is the triage/dedup key.
SOURCE_NAMES = ("gmail", "calendar", "tasks", "medium")

# Initial-seed-only defaults (an existing install's interval is changed
# via Settings, never by this). gmail/calendar/tasks are action-oriented
# and worth checking within the working day; medium is pure content
# discovery with no same-day urgency, so it defaults far less frequent.
DEFAULT_SOURCE_INTERVALS = {
    "gmail": 240,
    "calendar": 240,
    "tasks": 240,
    "medium": 1440,
}


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


# The _fetch_* helpers below catch everything on purpose: a source that
# fails (expired token, Google outage, an API change) degrades only its
# own lane and is listed in degraded_lanes — it never fails the brief.


def _fetch_gmail(credentials) -> tuple[list[RawItem], list[str]]:
    try:
        return GmailSource(credentials).fetch(), []
    except Exception:
        logger.exception("Gmail ingestion failed")
        return [], ["gmail"]


def _fetch_calendar(credentials) -> tuple[list[RawItem], list[dict], list[str]]:
    try:
        source = CalendarSource(credentials)
        items, calendar_rows = source.fetch_with_events()
    except Exception:
        logger.exception("Calendar ingestion failed")
        return [], [], ["calendar"]
    # Deliberate: copying event edits back onto tasks is a side job — if
    # it fails, today's calendar still made it in, so nothing is degraded.
    try:
        calendar_sync.pull_back_edits(source)
    except Exception:
        logger.exception("Pulling calendar edits back onto tasks failed")
    return items, calendar_rows, []


def _fetch_tasks(credentials) -> tuple[list[RawItem], list[str]]:
    try:
        return TasksSource(credentials).fetch(), []
    except Exception:
        logger.exception("Tasks ingestion failed")
        return [], ["tasks"]


def _fetch_medium() -> tuple[list[dict], list[str]]:
    try:
        return medium.fetch_and_rank(), []
    except Exception:
        logger.exception("Medium ingestion failed")
        return [], ["medium"]


def _triage(raw_items: list[RawItem], label: str, model=None) -> tuple[list[dict], list[str]]:
    """Rules first (newsletters, promotions, job alerts... decided without
    the LLM — see triage_rules.prefilter), then the LLM on what's left,
    then the post-LLM rule overrides. Returns (items, degraded). A rules
    bug never costs the pull: prefilter failing just sends everything to
    the LLM, and an override failing keeps the LLM's lanes."""
    try:
        for_llm, decided = triage_rules.prefilter(raw_items)
    except Exception:  # deliberate degradation — see docstring
        logger.exception("Triage pre-filter failed for %s", label)
        for_llm, decided = raw_items, []

    triaged: list[dict] = []
    degraded: list[str] = []
    if for_llm:
        try:
            triaged = triage.run(for_llm, model=model)
        # Deliberate: a provider outage or a malformed LLM reply marks
        # triage as degraded; the rule-decided items are still saved.
        except Exception:
            logger.exception("Triage failed for %s", label)
            degraded.append("triage")
        else:
            try:
                triage_rules.apply_rules(triaged, for_llm)
            except Exception:  # deliberate degradation — see docstring
                logger.exception("Applying triage rules failed for %s", label)
    return triaged + decided, degraded


def _prioritize() -> None:
    """High/Medium/Low for today's open tasks, after the pull is saved.
    Deliberate degradation: a failure here keeps every task's current
    priority and never costs the pull — it's recorded so the brief can
    say priorities are stale instead of failing silently."""
    try:
        prioritize.run()
    except Exception:
        logger.exception("Prioritizing today's tasks failed")
        queries.set_app_value("prioritize_failed_at", datetime.now(TZ).isoformat())
    else:
        queries.set_app_value("prioritize_failed_at", "")


def run(force: bool = False) -> None:
    """Full pipeline: every source, one shared triage call for the three
    that feed it, Medium's own ranking step layered in separately.
    """
    credentials = get_google_credentials()
    degraded: list[str] = []
    raw_items: list[RawItem] = []

    gmail_items, gmail_degraded = _fetch_gmail(credentials)
    raw_items += gmail_items
    degraded += gmail_degraded

    cal_items, calendar_rows, cal_degraded = _fetch_calendar(credentials)
    raw_items += cal_items
    degraded += cal_degraded

    task_items, task_degraded = _fetch_tasks(credentials)
    raw_items += task_items
    degraded += task_degraded

    triaged, triage_degraded = _triage(raw_items, "all sources")
    degraded += triage_degraded
    triaged, reading_degraded = reading.pick(triaged, raw_items)
    degraded += reading_degraded

    reading_items, medium_degraded = _fetch_medium()
    degraded += medium_degraded
    triaged += reading_items

    queries.save_triage_results(
        brief_date=_today(),
        triaged_items=triaged,
        calendar_events=calendar_rows,
        degraded_sources=degraded,
        sources_attempted=list(SOURCE_NAMES),
        force=force,
    )
    # Every source was attempted, so Settings' "Last pulled" should say so
    # — same as a single "Pull now", which stamps it degraded or not.
    for name in SOURCE_NAMES:
        queries.touch_source_last_pulled(name)
    _prioritize()
    logger.info(
        "Brief saved for %s: %d items triaged, degraded=%s",
        _today(),
        len(triaged),
        degraded or "none",
    )


def run_source(name: str) -> None:
    """Pulls + saves a single source — used by "Pull now" and the
    scheduler coordinator tick. gmail/calendar/tasks still run
    triage.run() on just their own items; medium has its own ranking
    step and skips the shared triage call entirely.
    """
    if name not in SOURCE_NAMES:
        raise ValueError(f"Unknown source: {name!r}")

    brief_date = _today()
    degraded: list[str] = []
    calendar_rows: list[dict] = []
    triaged: list[dict] = []

    if name == "medium":
        triaged, degraded = _fetch_medium()
    else:
        credentials = get_google_credentials()
        if name == "gmail":
            raw_items, degraded = _fetch_gmail(credentials)
        elif name == "calendar":
            raw_items, calendar_rows, degraded = _fetch_calendar(credentials)
        else:  # tasks
            raw_items, degraded = _fetch_tasks(credentials)

        if raw_items:
            triaged, triage_degraded = _triage(raw_items, f"source {name}")
            degraded += triage_degraded
            triaged, reading_degraded = reading.pick(triaged, raw_items)
            degraded += reading_degraded

    queries.save_triage_results(
        brief_date=brief_date,
        triaged_items=triaged,
        calendar_events=calendar_rows,
        degraded_sources=degraded,
        sources_attempted=[name],
        force=False,
    )
    queries.touch_source_last_pulled(name)
    _prioritize()
    logger.info("Pulled %s: %d items, degraded=%s", name, len(triaged), degraded or "none")


def seed_source_config() -> None:
    queries.seed_source_config(list(SOURCE_NAMES), overrides=DEFAULT_SOURCE_INTERVALS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Override dedup, resurface items")
    args = parser.parse_args()
    run(force=args.force)
