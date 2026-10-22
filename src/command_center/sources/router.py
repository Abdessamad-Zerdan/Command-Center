"""PATCH/POST for /settings/sources/{source_name} — each source's
enabled flag, pull interval and "Pull now". Moved out of app.py (over
the ~1,000-line limit) so the source settings live in one place.

Route collision check: the only POST here with a path param is
/settings/sources/{source_name}/pull, so a literal route added beside
it can't be shadowed by it.
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from command_center import auth, pipeline, queries, refresh

logger = logging.getLogger(__name__)

router = APIRouter()


class SourceConfigIn(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None


@router.patch("/settings/sources/{source_name}")
def update_source_config(source_name: str, payload: SourceConfigIn):
    if source_name not in pipeline.SOURCE_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_name!r}")
    queries.update_source_config(
        source_name, enabled=payload.enabled, interval_minutes=payload.interval_minutes
    )
    return {"ok": True}


@router.post("/settings/sources/pull-all")
def pull_all_sources():
    # One click for "the brief didn't generate": switch every source back
    # on (a disabled one is skipped by the scheduler, so it stays stale)
    # and start the same background full pull /rerun uses. Single-flight,
    # so a click during a running pull doesn't pull twice.
    for name in pipeline.SOURCE_NAMES:
        queries.update_source_config(name, enabled=True)
    if not auth.has_valid_credentials():
        logger.info("Pull all: sources enabled, but no Google credentials yet")
        return RedirectResponse(url="/settings", status_code=303)
    refresh.start()
    # The brief page shows "Building today's brief…" and reloads itself
    # when the run is done — honest progress instead of a silent wait here.
    return RedirectResponse(url="/brief", status_code=303)


@router.post("/settings/sources/{source_name}/pull")
def pull_source_now(source_name: str):
    if source_name not in pipeline.SOURCE_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_name!r}")
    # Medium needs no Google auth; gmail/calendar/tasks do — same gate /rerun
    # uses, so a pre-`make auth` click degrades instead of raising.
    if source_name != "medium" and not auth.has_valid_credentials():
        logger.info("Skipping pull for %s: no Google credentials yet", source_name)
        return RedirectResponse(url="/settings", status_code=303)
    try:
        pipeline.run_source(source_name)
    # Deliberate: the Settings page shows each source's last pull, so a
    # failed manual pull is logged and the user lands back there, not on
    # a 500.
    except Exception:
        logger.exception("Manual pull failed for source %s", source_name)
    return RedirectResponse(url="/settings", status_code=303)
