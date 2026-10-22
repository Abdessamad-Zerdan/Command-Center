"""The /map boards and cards, and which tasks belong to a card.

Part of the queries package — see queries/__init__.py."""

from datetime import datetime
from typing import Any

from command_center.config import TZ
from command_center.db import session


MAP_NODE_KINDS = ("label", "project", "target", "hackathon", "competition", "research")



def create_map_board(name: str) -> int:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        cursor = conn.execute("INSERT INTO map_boards (name, created_at) VALUES (?, ?)", (name, now))
        return cursor.lastrowid



def list_map_boards() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM map_boards ORDER BY created_at ASC, id ASC").fetchall()
        return [dict(row) for row in rows]



def get_map_board(board_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM map_boards WHERE id = ?", (board_id,)).fetchone()
        return dict(row) if row is not None else None



def get_or_create_default_board() -> dict[str, Any]:
    """/map always needs a board to render — this is what a brand-new
    install, or one where every board has since been deleted, falls
    back to. Created lazily on first visit rather than at install time,
    same reasoning as fixtures.seed being pull-triggered, not eager."""
    boards = list_map_boards()
    if boards:
        return boards[0]
    board_id = create_map_board("Board 1")
    return get_map_board(board_id)



def rename_map_board(board_id: int, name: str) -> bool:
    with session() as conn:
        cursor = conn.execute("UPDATE map_boards SET name = ? WHERE id = ?", (name, board_id))
        return cursor.rowcount > 0



def delete_map_board(board_id: int) -> bool:
    """Deletes a board and every card on it — explicit cleanup rather
    than relying on FK cascade, same reasoning as
    delete_registered_project."""
    with session() as conn:
        conn.execute(
            "UPDATE items SET map_node_id = NULL WHERE map_node_id IN (SELECT id FROM map_nodes WHERE board_id = ?)",
            (board_id,),
        )
        conn.execute("DELETE FROM map_nodes WHERE board_id = ?", (board_id,))
        cursor = conn.execute("DELETE FROM map_boards WHERE id = ?", (board_id,))
        return cursor.rowcount > 0



def create_map_node(
    board_id: int, kind: str, title: str = "", note: str = "", target_date: str | None = None,
    project_id: int | None = None, linked_label_id: int | None = None, x: float = 50.0, y: float = 50.0,
    competition_id: int | None = None,
) -> int:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO map_nodes (board_id, kind, title, note, target_date, project_id, "
            "linked_label_id, competition_id, x, y, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (board_id, kind, title, note, target_date, project_id, linked_label_id, competition_id, x, y, now),
        )
        return cursor.lastrowid



def get_map_node(node_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM map_nodes WHERE id = ?", (node_id,)).fetchone()
        return dict(row) if row is not None else None



# Which tasks "belong to" a card: tasks linked to the card itself, plus —
# for a card that stands for a project or a saved competition — every task
# linked to that project/competition. A task linked both ways is counted
# once (it's one row either way).
_NODE_TASKS_WHERE = (
    "(i.map_node_id = n.id "
    "OR (n.project_id IS NOT NULL AND i.project_id = n.project_id) "
    "OR (n.competition_id IS NOT NULL AND i.competition_id = n.competition_id) "
    "OR (n.pursuit_id IS NOT NULL AND i.pursuit_id = n.pursuit_id))"
)



def list_map_nodes(board_id: int) -> list[dict[str, Any]]:
    """Every card on one board, left-joined against registered_projects
    so a kind='project' card always reflects that project's current
    name and sprint rather than a stale copy — see map_nodes.project_id.
    open_tasks/done_tasks drive the little progress line on each card."""
    with session() as conn:
        rows = conn.execute(
            "SELECT n.*, p.name AS project_name, p.description AS project_description, "
            "p.current_sprint AS project_current_sprint, p.active AS project_active, "
            f"(SELECT COUNT(*) FROM items i WHERE {_NODE_TASKS_WHERE} AND i.status = 'pending') AS open_tasks, "
            f"(SELECT COUNT(*) FROM items i WHERE {_NODE_TASKS_WHERE} AND i.status = 'done') AS done_tasks "
            "FROM map_nodes n LEFT JOIN registered_projects p ON n.project_id = p.id "
            "WHERE n.board_id = ? ORDER BY n.id ASC",
            (board_id,),
        ).fetchall()
        return [dict(row) for row in rows]



def list_items_for_map_node(node_id: int) -> list[dict[str, Any]]:
    """Every task that belongs to a card (see _NODE_TASKS_WHERE), open
    ones first, then newest."""
    with session() as conn:
        rows = conn.execute(
            f"SELECT i.* FROM items i JOIN map_nodes n ON n.id = ? WHERE {_NODE_TASKS_WHERE} "
            "ORDER BY CASE i.status WHEN 'pending' THEN 0 ELSE 1 END, i.brief_date DESC, i.id DESC",
            (node_id,),
        ).fetchall()
        return [dict(row) for row in rows]



def list_linkable_map_cards() -> list[dict[str, Any]]:
    """Cards a task can belong to directly, across every board: anything
    but labels (headings, not work) and project cards (a task joins a
    project through items.project_id instead, so the project page sees
    it too)."""
    with session() as conn:
        rows = conn.execute(
            "SELECT n.id, n.kind, n.title, n.board_id, n.competition_id, n.pursuit_id, b.name AS board_name "
            "FROM map_nodes n JOIN map_boards b ON b.id = n.board_id "
            "WHERE n.kind NOT IN ('label', 'project') ORDER BY b.id ASC, n.title COLLATE NOCASE ASC"
        ).fetchall()
        return [dict(row) for row in rows]



def find_map_node_for_pursuit(pursuit_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM map_nodes WHERE pursuit_id = ? ORDER BY id ASC LIMIT 1", (pursuit_id,)
        ).fetchone()
        return dict(row) if row else None



def link_map_node_to_pursuit(node_id: int, pursuit_id: int) -> bool:
    with session() as conn:
        cursor = conn.execute("UPDATE map_nodes SET pursuit_id = ? WHERE id = ?", (pursuit_id, node_id))
        return cursor.rowcount > 0



def list_unlinked_competition_cards() -> list[dict[str, Any]]:
    """Competition/hackathon cards made straight on the Map that aren't
    saved on the Competitions page yet — offered there so saving one
    adopts the existing card instead of creating a duplicate."""
    with session() as conn:
        rows = conn.execute(
            "SELECT n.id, n.kind, n.title, n.note, n.target_date, b.name AS board_name "
            "FROM map_nodes n JOIN map_boards b ON b.id = n.board_id "
            "WHERE n.kind IN ('competition', 'hackathon') AND n.competition_id IS NULL "
            "ORDER BY n.title COLLATE NOCASE ASC"
        ).fetchall()
        return [dict(row) for row in rows]



def link_map_node_to_competition(node_id: int, competition_id: int) -> bool:
    """Makes a card stand for a competition — only a competition/hackathon
    card that isn't already standing for one."""
    with session() as conn:
        cursor = conn.execute(
            "UPDATE map_nodes SET competition_id = ? WHERE id = ? "
            "AND kind IN ('competition', 'hackathon') AND competition_id IS NULL",
            (competition_id, node_id),
        )
        return cursor.rowcount > 0



def find_map_node_for_competition(competition_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM map_nodes WHERE competition_id = ? ORDER BY id ASC LIMIT 1", (competition_id,)
        ).fetchone()
        return dict(row) if row else None



def list_labels_on_board(board_id: int) -> list[dict[str, Any]]:
    """kind='label' cards on one board — feeds the "link to label"
    picker and validates a submitted linked_label_id actually belongs
    to the board being edited."""
    with session() as conn:
        rows = conn.execute(
            "SELECT id, title FROM map_nodes WHERE board_id = ? AND kind = 'label' ORDER BY title ASC",
            (board_id,),
        ).fetchall()
        return [dict(row) for row in rows]



def list_pinnable_projects(board_id: int) -> list[dict[str, Any]]:
    """Active projects not already pinned onto this specific board —
    the same project can be pinned on more than one board (an ongoing
    project spanning both a September and October board), so "already
    pinned" is scoped per board, not global."""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM registered_projects WHERE active = 1 AND id NOT IN "
            "(SELECT project_id FROM map_nodes WHERE project_id IS NOT NULL AND board_id = ?) "
            "ORDER BY name ASC",
            (board_id,),
        ).fetchall()
        return [dict(row) for row in rows]



def update_map_node_position(node_id: int, x: float, y: float) -> bool:
    with session() as conn:
        cursor = conn.execute("UPDATE map_nodes SET x = ?, y = ? WHERE id = ?", (x, y, node_id))
        return cursor.rowcount > 0



def update_map_node(node_id: int, title: str, note: str, target_date: str | None) -> bool:
    """Edits a card's own text — restricted to non-project cards
    (`project_id IS NULL`) since a kind='project' card's title/note
    always mirrors the linked project instead of storing its own."""
    with session() as conn:
        cursor = conn.execute(
            "UPDATE map_nodes SET title = ?, note = ?, target_date = ? WHERE id = ? AND project_id IS NULL",
            (title, note, target_date, node_id),
        )
        return cursor.rowcount > 0



def update_map_node_label_link(node_id: int, linked_label_id: int | None) -> bool:
    """Sets or clears which label a card points at — unlike
    update_map_node above, this isn't guarded by project_id IS NULL: a
    pinned project's title/note/target_date stay locked to the project
    it mirrors, but the project card itself should still be linkable
    to a label like any other kind."""
    with session() as conn:
        cursor = conn.execute(
            "UPDATE map_nodes SET linked_label_id = ? WHERE id = ?", (linked_label_id, node_id)
        )
        return cursor.rowcount > 0



def delete_map_node(node_id: int) -> bool:
    """Removes a card from the map. For kind='project' cards this only
    unpins it — the underlying registered_projects row is untouched.
    Deleting a label first unlinks anything pointing to it instead of
    leaving a dangling linked_label_id (a no-op UPDATE for any node
    that isn't a label, since nothing else is ever a valid link target)."""
    with session() as conn:
        conn.execute("UPDATE map_nodes SET linked_label_id = NULL WHERE linked_label_id = ?", (node_id,))
        conn.execute("UPDATE items SET map_node_id = NULL WHERE map_node_id = ?", (node_id,))
        cursor = conn.execute("DELETE FROM map_nodes WHERE id = ?", (node_id,))
        return cursor.rowcount > 0
