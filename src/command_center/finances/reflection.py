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

from datetime import date, datetime

from command_center import triage
from command_center.config import TZ
from command_center.db import session
from command_center.finances import aggregations
from command_center.triage import TriageProviderError

SYSTEM_PROMPT = ""  # withheld from the public showcase - see README.md


def _format_month_block(label_text: str, summary: dict, breakdown: dict) -> str:
    raise TriageProviderError("_format_month_block: withheld from the public showcase - see README.md")


def _build_user_message(start: date, end: date, prev_start: date, prev_end: date) -> str:
    raise TriageProviderError("_build_user_message: withheld from the public showcase - see README.md")


def generate_reflection(start: date, end: date) -> str:
    """Pure Groq call — no cache read/write. Raises TriageProviderError,
    uncaught, on any Groq failure."""
    raise TriageProviderError("generate_reflection: withheld from the public showcase - see README.md")


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


def get_saved_reflection(month_start: date) -> str | None:
    """The last reflection written for this month, without calling the
    model — what the page shows until the user presses Regenerate."""
    cached = _get_cached(month_start)
    return cached["generated_text"] if cached else None


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
