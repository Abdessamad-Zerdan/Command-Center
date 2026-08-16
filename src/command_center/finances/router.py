"""GET /finances — month-based spending/income/savings dashboard: entry
form, summary cards, category breakdown + trend charts, and an AI
reflection. Lives only in the More dropdown (see base.html) — not a
first-interface feature, matching /history/report's own placement.
"""

import logging
from datetime import date as date_type
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from command_center import queries
from command_center.config import FINANCE_CATEGORIES, FINANCE_TYPES, TZ
from command_center.finances import aggregations, charts, reflection
from command_center.history import periods
from command_center.triage import TriageProviderError

logger = logging.getLogger(__name__)

router = APIRouter()

# A fresh Jinja2Templates instance, same reasoning as history/router.py's
# own — app.py imports this router, so importing app.py's instance back
# here would be circular.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

TREND_MONTHS = 6


@router.get("/finances")
def finances_view(request: Request, date: str | None = None):
    try:
        anchor = periods.parse_anchor(date)
        start, end = periods.period_bounds("month", anchor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    summary = aggregations.monthly_summary(start, end)
    breakdown = aggregations.category_breakdown(start, end)
    trend = aggregations.monthly_trend(start, num_months=TREND_MONTHS)
    entries = queries.list_finance_entries(start.isoformat(), end.isoformat())

    prev_anchor = periods.shift_anchor("month", anchor, -1)
    next_anchor = periods.shift_anchor("month", anchor, 1)

    reflection_text = None
    try:
        reflection_text = reflection.get_or_generate_reflection(start, end)
    except TriageProviderError:
        logger.warning("Finance reflection generation failed for %s", start)

    return templates.TemplateResponse(
        request,
        "finances.html",
        {
            "anchor": anchor.isoformat(),
            "label": periods.label("month", start, end),
            "prev_url": f"/finances?date={prev_anchor.isoformat()}",
            "next_url": f"/finances?date={next_anchor.isoformat()}",
            "summary": summary,
            "has_breakdown": bool(breakdown),
            "breakdown_svg": charts.render_category_bars_svg(breakdown),
            "trend_svg": charts.render_trend_svg(trend),
            "entries": entries,
            "categories": FINANCE_CATEGORIES,
            "types": FINANCE_TYPES,
            "today": datetime.now(TZ).date().isoformat(),
            "reflection_text": reflection_text,
        },
    )


@router.post("/finances/entries")
def create_finance_entry(
    amount: float = Form(...),
    category: str = Form(...),
    entry_type: str = Form(..., alias="type"),
    entry_date: str = Form(...),
    note: str = Form(""),
    date: str = Form(...),
):
    if category not in FINANCE_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown category: {category!r}")
    if entry_type not in FINANCE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown type: {entry_type!r}")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    try:
        date_type.fromisoformat(entry_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date: {entry_date!r}") from exc

    queries.create_finance_entry(amount, category, entry_type, entry_date, note=note.strip())
    return RedirectResponse(url=f"/finances?date={date}", status_code=303)


@router.post("/finances/regenerate-reflection")
def regenerate_reflection(date: str = Form(...)):
    try:
        anchor = periods.parse_anchor(date)
        start, end = periods.period_bounds("month", anchor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        reflection.get_or_generate_reflection(start, end, force=True)
    except TriageProviderError:
        # Swallow, same as /history/report's regenerate route — no
        # flash-message system exists anywhere in this app.
        logger.exception("Manual finance reflection regenerate failed")

    return RedirectResponse(url=f"/finances?date={anchor.isoformat()}", status_code=303)
