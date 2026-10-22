"""Finance entries.

Part of the queries package — see queries/__init__.py."""

from datetime import datetime
from typing import Any

from command_center.config import TZ
from command_center.db import session


def create_finance_entry(amount: float, category: str, type_: str, entry_date: str, note: str = "") -> int:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO finance_entries (amount, category, note, entry_date, type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (amount, category, note, entry_date, type_, now),
        )
        return cursor.lastrowid



def list_finance_entries(start: str, end: str) -> list[dict[str, Any]]:
    """entry_date in [start, end), both ISO date strings — same half-open
    convention as history/aggregations.py. Newest first."""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM finance_entries WHERE entry_date >= ? AND entry_date < ? "
            "ORDER BY entry_date DESC, id DESC",
            (start, end),
        ).fetchall()
        return [dict(row) for row in rows]



def export_finance_entries() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM finance_entries ORDER BY entry_date ASC, id ASC").fetchall()
        return [dict(row) for row in rows]
