"""AI-generated reflection text for /history/report — a short, honest
summary of one period's task activity compared to the period immediately
before it. Grounded strictly in the numbers Part 1's aggregations.py
already computes; no task titles/content are ever sent to the model.

Caching (reflection_cache table): a period's aggregation numbers are a
deterministic function of exactly the task_events rows with timestamp
< the period's own `end` (every aggregations.py query is bounded that
way, directly or transitively — see the Part 2 plan for the full proof).
Events are append-only and always timestamped "now" at insert, so once
real time passes a period's `end`, no future event can ever land inside
that window again — the count of such rows is therefore a sufficient,
free invalidation signal: unchanged count means the numbers could not
have changed, and a fully-elapsed period's count can never change again,
making its cached reflection permanently valid with no special-casing.
"""

from datetime import date, datetime, timedelta

from command_center import queries, triage
from command_center.config import LANES, TZ
from command_center.db import session
from command_center.history import aggregations, periods

SYSTEM_PROMPT = """You are writing a short reflection for a personal productivity dashboard, summarizing one time period's task activity compared to the period immediately before it.

Rules:
- Base every statement strictly on the numbers given to you. Do not invent, assume, or guess anything not present in the numbers.
- Never speculate about *why* something changed — no guesses about the person's mood, workload, business, stress, or motivation. State only what the numbers show, not why they might have happened.
- Write 2 to 4 sentences of plain prose. No headings, no bullet points, no markdown formatting, no emoji.
- Mention at least one specific number or percentage from the data to ground the observation — a vague statement like "things improved" with no number attached is not acceptable.
- If both periods show zero created and zero completed tasks, say so plainly in one sentence and do not invent a trend.
- Tone: honest, calm, observational — like a colleague pointing at a chart, not a coach or cheerleader."""


def _collect(start: date, end: date) -> dict:
    return {
        "completion": aggregations.completion_rate(start, end),
        "same_day": aggregations.same_day_resolution_rate(start, end),
        "avg_time": aggregations.avg_time_to_complete(start, end),
        "rollover": aggregations.rollover_count(start, end),
    }


def _lane_order(current: dict, previous: dict) -> list[str]:
    present = set(current["by_lane"]) | set(previous["by_lane"])
    ordered = [lane for lane in LANES if lane in present]
    if "unknown" in present:
        ordered.append("unknown")
    return ordered


def _format_period_block(label_text: str, data: dict, lanes: list[str]) -> str:
    completion = data["completion"]
    by_lane = completion["by_lane"]
    lane_labels = queries.get_lane_labels()

    def _counts(field: str) -> str:
        parts = [f"{lane_labels.get(l, 'Unknown')}: {by_lane.get(l, {}).get(field, 0)}" for l in lanes]
        return ", ".join(parts)

    avg_time = data["avg_time"]
    avg_parts = [
        f"{lane_labels.get(l, 'Unknown')} {round(avg_time[l]['avg_hours'], 1)}h (n={avg_time[l]['n']})"
        for l in lanes
        if l in avg_time
    ]
    avg_text = ", ".join(avg_parts) if avg_parts else "no completions"

    return (
        f"{label_text}:\n"
        f"- Created: {completion['total_created']} total ({_counts('created')})\n"
        f"- Completed: {completion['total_completed']} total ({_counts('completed')})\n"
        f"- Same-day resolution (urgent items): {round(data['same_day'] * 100)}%\n"
        f"- Rolled over from before this period, still open at period end: {data['rollover']}\n"
        f"- Avg. time to complete: {avg_text}"
    )


def _build_user_message(
    period: str, start: date, end: date, prev_start: date, prev_end: date, current: dict, previous: dict
) -> str:
    period_range = f"{start.isoformat()} to {(end - timedelta(days=1)).isoformat()}"
    prev_range = f"{prev_start.isoformat()} to {(prev_end - timedelta(days=1)).isoformat()}"
    header = f"Period: {period} ({period_range}, inclusive)\nPrevious period: {period} ({prev_range}, inclusive)"

    no_activity = (
        current["completion"]["total_created"] == 0
        and current["completion"]["total_completed"] == 0
        and previous["completion"]["total_created"] == 0
        and previous["completion"]["total_completed"] == 0
    )
    if no_activity:
        return (
            f"{header}\n\n"
            "No task activity (no items created or completed) was recorded in either period.\n\n"
            "Write the reflection now."
        )

    lanes = _lane_order(current["completion"], previous["completion"])
    current_block = _format_period_block("Current period", current, lanes)
    previous_block = _format_period_block("Previous period", previous, lanes)
    return f"{header}\n\n{current_block}\n\n{previous_block}\n\nWrite the reflection now, comparing current to previous."


def generate_reflection(period: str, start: date, end: date) -> str:
    """Pure Groq call — no cache read/write. Raises ValueError for
    period="day" (reflections are week/month/year only — too little data
    for a meaningful pattern in a single day). Raises TriageProviderError,
    uncaught, on any Groq failure."""
    if period == "day":
        raise ValueError("Reflections are not available for period='day'")

    prev_start, prev_end = periods.period_bounds(period, start - timedelta(days=1))
    current = _collect(start, end)
    previous = _collect(prev_start, prev_end)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_user_message(period, start, end, prev_start, prev_end, current, previous),
        },
    ]
    return triage.run_groq_chat(messages, max_tokens=300).strip()


def _event_count(end: date) -> int:
    with session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE timestamp < ?", (end.isoformat(),)
        ).fetchone()
    return row["n"]


def _get_cached(period: str, start: date) -> dict | None:
    with session() as conn:
        row = conn.execute(
            "SELECT generated_text, event_count FROM reflection_cache "
            "WHERE period_type = ? AND period_start = ?",
            (period, start.isoformat()),
        ).fetchone()
    return dict(row) if row else None


def _store(period: str, start: date, text: str, event_count: int) -> None:
    with session() as conn:
        conn.execute(
            """
            INSERT INTO reflection_cache (period_type, period_start, generated_text, event_count, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(period_type, period_start) DO UPDATE SET
                generated_text = excluded.generated_text,
                event_count = excluded.event_count,
                created_at = excluded.created_at
            """,
            (period, start.isoformat(), text, event_count, datetime.now(TZ).isoformat()),
        )


def get_or_generate_reflection(period: str, start: date, end: date, force: bool = False) -> str:
    """The function everything outside this module should call. Raises
    ValueError for period="day" — defensive, protects the POST regenerate
    route (which also takes period from a form field) as well as the GET
    route's own gate."""
    if period == "day":
        raise ValueError("Reflections are not available for period='day'")

    current_count = _event_count(end)

    if not force:
        cached = _get_cached(period, start)
        if cached is not None and cached["event_count"] == current_count:
            return cached["generated_text"]

    text = generate_reflection(period, start, end)  # TriageProviderError propagates uncaught
    _store(period, start, text, current_count)
    return text
