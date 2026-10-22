"""Research topics and targets (the Research / Targets pages).

Part of the queries package — see queries/__init__.py."""

from datetime import datetime
from typing import Any

from command_center.config import TZ
from command_center.db import session


# --- research topics and targets (/research, /targets) ------------------------

PURSUIT_KINDS = ("research", "target")



PURSUIT_STATUSES = {
    "research": ("idea", "active", "done"),
    "target": ("planned", "in_progress", "achieved"),
}



PURSUIT_FIELDS = ("title", "notes", "status", "target_date", "url")



def create_pursuit(kind: str, fields: dict[str, Any]) -> int:
    now = datetime.now(TZ).isoformat()
    values = {"status": PURSUIT_STATUSES[kind][0], **{k: v for k, v in fields.items() if k in PURSUIT_FIELDS}}
    columns = list(values)
    with session() as conn:
        cursor = conn.execute(
            f"INSERT INTO pursuits (kind, {', '.join(columns)}, created_at, updated_at) "
            f"VALUES (?, {', '.join('?' for _ in columns)}, ?, ?)",
            [kind] + [values[c] for c in columns] + [now, now],
        )
        return cursor.lastrowid



def list_pursuits(kind: str) -> list[dict[str, Any]]:
    """One page's entries, each with its open-task count and the id of its
    Map card (if it has one)."""
    with session() as conn:
        rows = conn.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM items i WHERE i.pursuit_id = p.id AND i.status = 'pending') AS open_tasks, "
            "(SELECT MIN(n.id) FROM map_nodes n WHERE n.pursuit_id = p.id) AS map_node_id "
            "FROM pursuits p WHERE p.kind = ? ORDER BY p.id DESC",
            (kind,),
        ).fetchall()
        return [dict(row) for row in rows]



def get_pursuit(pursuit_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM pursuits WHERE id = ?", (pursuit_id,)).fetchone()
        return dict(row) if row else None



def update_pursuit(pursuit_id: int, fields: dict[str, Any]) -> bool:
    """Partial update. Its Map cards mirror the title/notes/date, so
    they're rewritten in the same transaction."""
    columns = [c for c in PURSUIT_FIELDS if c in fields]
    if not columns:
        return get_pursuit(pursuit_id) is not None
    with session() as conn:
        cursor = conn.execute(
            f"UPDATE pursuits SET {', '.join(f'{c} = ?' for c in columns)}, updated_at = ? WHERE id = ?",
            [fields[c] for c in columns] + [datetime.now(TZ).isoformat(), pursuit_id],
        )
        if cursor.rowcount == 0:
            return False
        mirrored = {"title": "title", "notes": "note", "target_date": "target_date"}
        sets = [(mirrored[c], fields[c] if c != "notes" else (fields[c] or "")) for c in columns if c in mirrored]
        if sets:
            conn.execute(
                f"UPDATE map_nodes SET {', '.join(f'{col} = ?' for col, _ in sets)} WHERE pursuit_id = ?",
                [v for _, v in sets] + [pursuit_id],
            )
        return True



def set_pursuit_cover(pursuit_id: int, stored_name: str | None, content_type: str | None) -> bool:
    with session() as conn:
        cursor = conn.execute(
            "UPDATE pursuits SET cover_name = ?, cover_type = ?, updated_at = ? WHERE id = ?",
            (stored_name, content_type, datetime.now(TZ).isoformat(), pursuit_id),
        )
        return cursor.rowcount > 0



def delete_pursuit(pursuit_id: int) -> bool:
    """Its tasks stay (unlinked). Its Map cards go too: a research/target
    card always stands for an entry, and an orphaned one would just be
    adopted back as a new entry on the next start (see
    db._adopt_map_pursuit_cards)."""
    with session() as conn:
        conn.execute("UPDATE items SET pursuit_id = NULL WHERE pursuit_id = ?", (pursuit_id,))
        card_ids = "(SELECT id FROM map_nodes WHERE pursuit_id = ?)"
        conn.execute(f"UPDATE map_nodes SET linked_label_id = NULL WHERE linked_label_id IN {card_ids}", (pursuit_id,))
        conn.execute(f"UPDATE items SET map_node_id = NULL WHERE map_node_id IN {card_ids}", (pursuit_id,))
        conn.execute("DELETE FROM map_nodes WHERE pursuit_id = ?", (pursuit_id,))
        cursor = conn.execute("DELETE FROM pursuits WHERE id = ?", (pursuit_id,))
        return cursor.rowcount > 0



def pursuits_tab_counts() -> dict[str, int]:
    """The small numbers on the Pursuits tab row: what's still in play on
    each page (passed competitions, finished research, achieved targets
    and inactive projects don't count)."""
    with session() as conn:
        def one(sql: str) -> int:
            return conn.execute(sql).fetchone()[0]
        return {
            "competitions": one("SELECT COUNT(*) FROM competitions WHERE status != 'passed'"),
            "research": one("SELECT COUNT(*) FROM pursuits WHERE kind = 'research' AND status != 'done'"),
            "targets": one("SELECT COUNT(*) FROM pursuits WHERE kind = 'target' AND status != 'achieved'"),
            "projects": one("SELECT COUNT(*) FROM registered_projects WHERE active = 1"),
        }
