"""AI-generated reflection text for /finances — a short, honest summary
of one month's spending compared to the month immediately before it.
Grounded strictly in the numbers finances/aggregations.py already
computes; entry notes are never sent to the model.

Caching (finance_reflection_cache table): mirrors history/reflection.py's
invalidation strategy. Entries are add-only this pass (no edit/delete —
see the finances feature's scope), so a month's numbers are a
deterministic function of exactly the finance_entries rows with
entry_date < that month's own `end`. That row count is therefore a free,
sufficient invalidation signal — unchanged count means the numbers could
not have changed. If edit/delete is ever added, this invalidation
strategy needs revisiting (a count alone would no longer prove the sums
are unchanged).
"""

from datetime import date, datetime, timedelta

from command_center import triage
from command_center.config import FINANCE_CATEGORIES, TZ
from command_center.db import session
from command_center.finances import aggregations
from command_center.history import periods

SYSTEM_PROMPT = """You are writing a short reflection for a personal finance dashboard, summarizing one month's spending compared to the month immediately before it.

Rules:
- Base every statement strictly on the numbers given to you. Do not invent, assume, or guess anything not present in the numbers.
- Never speculate about *why* spending changed — no guesses about the person's habits, circumstances, income, or decisions. State only what the numbers show, not why they might have happened.
- Never give financial advice, budgeting suggestions, warnings, or recommendations of any kind — describe the numbers only.
- Write 2 to 4 sentences of plain prose. No headings, no bullet points, no markdown formatting, no emoji.
- Mention at least one specific number or amount from the data to ground the observation.
- If both months show no entries at all, say so plainly in one sentence and do not invent a trend.
- Tone: honest, calm, observational — like a colleague pointing at a chart, not an advisor."""


def _format_month_block(label_text: str, summary: dict, breakdown: dict) -> str:
    breakdown_parts = [
        f"{cat}: {breakdown[cat]:.2f}" for cat in FINANCE_CATEGORIES if breakdown.get(cat)
    ]
    breakdown_text = ", ".join(breakdown_parts) if breakdown_parts else "no spending recorded"
    return (
        f"{label_text}:\n"
        f"- Total spent: {summary['total_spent']:.2f}\n"
        f"- Total income: {summary['total_income']:.2f}\n"
        f"- Total saved: {summary['total_saved']:.2f}\n"
        f"- Net delta (income minus spent): {summary['net_delta']:.2f}\n"
        f"- Spending by category: {breakdown_text}"
    )


def _build_user_message(start: date, end: date, prev_start: date, prev_end: date) -> str:
    current_summary = aggregations.monthly_summary(start, end)
    current_breakdown = aggregations.category_breakdown(start, end)
    previous_summary = aggregations.monthly_summary(prev_start, prev_end)
    previous_breakdown = aggregations.category_breakdown(prev_start, prev_end)

    header = f"Month: {start.strftime('%B %Y')}\nPrevious month: {prev_start.strftime('%B %Y')}"

    no_activity = (
        current_summary["total_spent"] == 0
        and current_summary["total_income"] == 0
        and current_summary["total_saved"] == 0
        and previous_summary["total_spent"] == 0
        and previous_summary["total_income"] == 0
        and previous_summary["total_saved"] == 0
    )
    if no_activity:
        return f"{header}\n\nNo entries were recorded in either month.\n\nWrite the reflection now."

    current_block = _format_month_block("Current month", current_summary, current_breakdown)
    previous_block = _format_month_block("Previous month", previous_summary, previous_breakdown)
    return f"{header}\n\n{current_block}\n\n{previous_block}\n\nWrite the reflection now, comparing current to previous."


def generate_reflection(start: date, end: date) -> str:
    """Pure Groq call — no cache read/write. Raises TriageProviderError,
    uncaught, on any Groq failure."""
    prev_start, prev_end = periods.period_bounds("month", start - timedelta(days=1))
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(start, end, prev_start, prev_end)},
    ]
    return triage.run_groq_chat(messages, max_tokens=300).strip()


def _entry_count(end: date) -> int:
    with session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM finance_entries WHERE entry_date < ?", (end.isoformat(),)
        ).fetchone()
    return row["n"]


def _get_cached(month_start: date) -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT generated_text, entry_count FROM finance_reflection_cache WHERE month_start = ?",
            (month_start.isoformat(),),
        ).fetchone()
    return dict(row) if row else None


def _store(month_start: date, text: str, entry_count: int) -> None:
    with session() as conn:
        conn.execute(
            """
            INSERT INTO finance_reflection_cache (month_start, generated_text, entry_count, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(month_start) DO UPDATE SET
                generated_text = excluded.generated_text,
                entry_count = excluded.entry_count,
                created_at = excluded.created_at
            """,
            (month_start.isoformat(), text, entry_count, datetime.now(TZ).isoformat()),
        )


def get_or_generate_reflection(start: date, end: date, force: bool = False) -> str:
    """The function everything outside this module should call."""
    current_count = _entry_count(end)

    if not force:
        cached = _get_cached(start)
        if cached is not None and cached["entry_count"] == current_count:
            return cached["generated_text"]

    text = generate_reflection(start, end)  # TriageProviderError propagates uncaught
    _store(start, text, current_count)
    return text
