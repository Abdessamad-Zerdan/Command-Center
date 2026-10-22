"""AI-written reflection for /history/report — what the person actually
did in one period, compared with the period before.

It used to see counts only and could only say them back ("9 of 12
completed"). It now also gets the work itself (work_detail.py): the tasks
finished, Pomodoro focus time per task, and what stayed open all period.
Task titles go to the model for this — the owner's choice; never notes,
mail or attachments.

Tokens: nothing is generated when the page opens. The report shows the
saved text, or a "Write my reflection" button; only that button (POST
.../regenerate-reflection) calls the model.

Caching (reflection_cache table): a period's inputs are a deterministic
function of the task_events and pomodoro_sessions rows before the
period's `end`. Events are append-only and timestamped "now"; sessions
start "now" and only grow until finished. So a fingerprint of those rows
(_activity_signal) changing is exactly "the reflection may be out of
date", and a fully elapsed period's fingerprint never changes again.
"""

import hashlib
from datetime import date, datetime

from command_center import triage
from command_center.config import LANES, TZ
from command_center.db import session
from command_center.history import work_detail
from command_center.triage import TriageProviderError

SYSTEM_PROMPT = ""  # withheld from the public showcase - see README.md


def _lane_order(current: dict, previous: dict) -> list[str]:
    present = set(current["by_lane"]) | set(previous["by_lane"])
    ordered = [lane for lane in LANES if lane in present]
    if "unknown" in present:
        ordered.append("unknown")
    return ordered


def _format_period_block(label_text: str, data: dict, lanes: list[str]) -> str:
    raise TriageProviderError("_format_period_block: withheld from the public showcase - see README.md")


def _build_user_message(
    period: str, start: date, end: date, prev_start: date, prev_end: date, current: dict, previous: dict
) -> str:
    raise TriageProviderError("_build_user_message: withheld from the public showcase - see README.md")


def _format_work(label_text: str, detail: dict | None, with_lists: bool) -> str:
    """The work itself (work_detail.collect). The previous period gets
    totals only — enough to compare against, without doubling the
    prompt."""
    raise TriageProviderError("_format_work: withheld from the public showcase - see README.md")


def generate_reflection(period: str, start: date, end: date) -> str:
    """Pure Groq call — no cache read/write. Raises ValueError for
    period="day" (reflections are week/month/year only — too little data
    for a meaningful pattern in a single day). Raises TriageProviderError,
    uncaught, on any Groq failure."""
    raise TriageProviderError("generate_reflection: withheld from the public showcase - see README.md")


def _event_count(end: date) -> int:
    """Fingerprint of everything the reflection reads before `end`: task
    events (append-only) plus Pomodoro sessions (count and seconds — a
    session only ever grows). Stored in reflection_cache.event_count; a
    different value means the saved text may be out of date. Kept to 15
    hex digits so it fits SQLite's 64-bit integer."""
    with session() as conn:
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE timestamp < ?", (end.isoformat(),)
        ).fetchone()["n"]
        focus = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(elapsed_seconds), 0) AS s FROM pomodoro_sessions WHERE started_at < ?",
            (end.isoformat(),),
        ).fetchone()
    if focus["n"] == 0:
        return events  # unchanged meaning for a period with no Pomodoro data
    digest = hashlib.sha256(f"{events}:{focus['n']}:{focus['s']}".encode()).hexdigest()
    return int(digest[:15], 16)


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


def get_saved_reflection(period: str, start: date, end: date) -> dict | None:
    """What the report page shows — never calls the model. None if no
    reflection was ever written for this period; otherwise its text, when
    it was written, and whether activity has changed since (stale)."""
    if period == "day":
        return None
    with session() as conn:
        row = conn.execute(
            "SELECT generated_text, event_count, created_at FROM reflection_cache "
            "WHERE period_type = ? AND period_start = ?",
            (period, start.isoformat()),
        ).fetchone()
    if row is None:
        return None
    written = datetime.fromisoformat(row["created_at"]).astimezone(TZ)
    return {
        "text": row["generated_text"],
        "written_at": written.strftime("%d %b, %H:%M").lstrip("0"),
        "stale": row["event_count"] != _event_count(end),
    }


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
