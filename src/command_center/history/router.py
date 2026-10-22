"""GET /history/report — the event-log aggregation/charts view. Deliberately
a separate path from the existing /history and /history/{brief_date}
routes (a list of past brief dates, and a per-day brief snapshot,
respectively) — this is a different feature (stats over task_events, not
a brief re-render) and must not collide with them.

Routing-order note: /history/{brief_date} is a single-segment path param
that would also match a request for "/history/report" (capturing
brief_date="report") if it were registered first — Starlette matches
routes in registration order, first match wins. app.py must include this
router at the same early point it already includes assistant_router
(before its own @app.get("/history/{brief_date}") decorator executes),
or /history/report would 404 through the wrong handler. See
tests/test_history_report_route.py for the regression test on this.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from command_center import queries
from command_center.config import LANES
from command_center.history import aggregations, charts, periods, reflection
from command_center.triage import TriageProviderError

logger = logging.getLogger(__name__)

router = APIRouter()

# A fresh Jinja2Templates instance rather than importing app.py's — app.py
# needs to import this router, so importing back from here would be
# circular. Same directory, same plain (no custom filters) setup as
# app.py's own instance.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_GRANULARITY = {"day": "hour", "week": "day", "month": "day", "year": "month"}


@router.get("/history/report")
def history_report(request: Request, period: str = "week", date: str | None = None, reflection_error: int = 0):
    if period not in periods.PERIODS:
        raise HTTPException(status_code=400, detail=f"Unknown period: {period!r}")
    try:
        anchor = periods.parse_anchor(date)
        start, end = periods.period_bounds(period, anchor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    completion = aggregations.completion_rate(start, end)
    same_day = aggregations.same_day_resolution_rate(start, end)
    avg_time = aggregations.avg_time_to_complete(start, end)
    rollover = aggregations.rollover_count(start, end)
    trend = aggregations.completion_trend(start, end, _GRANULARITY[period])

    current_streak = aggregations.current_streak()
    all_time_completed = aggregations.all_time_completed()
    best_period = aggregations.best_period(period)
    pop_delta = aggregations.period_over_period_delta(period, start, end)

    prev_anchor = periods.shift_anchor(period, anchor, -1)
    next_anchor = periods.shift_anchor(period, anchor, 1)

    # Opening the page never calls the model (tokens): it shows the saved
    # reflection, or a button that writes one (regenerate_reflection).
    # Day view has no reflection — the template's period != "day" guard
    # omits the section.
    saved_reflection = reflection.get_saved_reflection(period, start, end)

    return templates.TemplateResponse(
        request,
        "history_report.html",
        {
            "period": period,
            "periods": periods.PERIODS,
            "anchor": anchor.isoformat(),
            "label": periods.label(period, start, end),
            "prev_url": f"/history/report?period={period}&date={prev_anchor.isoformat()}",
            "next_url": f"/history/report?period={period}&date={next_anchor.isoformat()}",
            "completion": completion,
            "same_day_resolution_rate": same_day,
            "avg_time_to_complete": avg_time,
            "rollover_count": rollover,
            "lane_labels": queries.get_lane_labels(),
            "lanes_order": LANES,
            "trend_svg": charts.render_trend_svg(trend),
            "bars_svg": charts.render_lane_bars_svg(completion),
            "reflection": saved_reflection,
            # Set by regenerate_reflection's redirect when writing failed —
            # said on the page instead of silently showing the old state.
            "reflection_error": bool(reflection_error),
            "current_streak": current_streak,
            "all_time_completed": all_time_completed,
            "best_period": best_period,
            "period_over_period_delta": pop_delta,
        },
    )


@router.post("/history/report/regenerate-reflection")
def regenerate_reflection(period: str = Form(...), date: str = Form(...)):
    if period not in periods.PERIODS or period == "day":
        raise HTTPException(status_code=400, detail=f"Reflection not available for period={period!r}")
    try:
        anchor = periods.parse_anchor(date)
        start, end = periods.period_bounds(period, anchor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    url = f"/history/report?period={period}&date={anchor.isoformat()}"
    try:
        reflection.get_or_generate_reflection(period, start, end, force=True)
    except TriageProviderError:
        # The page keeps whatever was saved before and says writing failed
        # (reflection_error) — no flash-message system, so a query flag.
        logger.exception("Writing the reflection failed")
        url += "&reflection_error=1"
    return RedirectResponse(url=url, status_code=303)
