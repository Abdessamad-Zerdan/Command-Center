"""Registered projects (the Projects page).

Part of the queries package — see queries/__init__.py."""

from datetime import datetime
from typing import Any

from command_center.config import TZ
from command_center.db import session


def create_registered_project(
    name: str, path: str, description: str = "", run_command: str = "", app_url: str = ""
) -> int:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO registered_projects (name, path, description, run_command, app_url, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (name, path, description, run_command or None, app_url or None, now),
        )
        return cursor.lastrowid



def list_registered_projects(active_only: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM registered_projects"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY name ASC"
    with session() as conn:
        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]



def get_project_card_stats() -> dict[int, dict[str, Any]]:
    """{project_id: {open_tasks, map_node_id}} for the Projects grid —
    absent means no open tasks and no Map card."""
    stats: dict[int, dict[str, Any]] = {}
    with session() as conn:
        for row in conn.execute(
            "SELECT project_id, COUNT(*) AS n FROM items "
            "WHERE project_id IS NOT NULL AND status = 'pending' GROUP BY project_id"
        ):
            stats.setdefault(row["project_id"], {})["open_tasks"] = row["n"]
        for row in conn.execute(
            "SELECT project_id, MIN(id) AS node_id FROM map_nodes WHERE project_id IS NOT NULL GROUP BY project_id"
        ):
            stats.setdefault(row["project_id"], {})["map_node_id"] = row["node_id"]
    return stats



def get_registered_project(project_id: int) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM registered_projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row is not None else None



def update_registered_project(
    project_id: int,
    description: str | None = None,
    current_sprint: str | None = None,
    sprint_goal: str | None = None,
    blockers: str | None = None,
    target_date: str | None = None,
    run_command: str | None = None,
    app_url: str | None = None,
    active: bool | None = None,
) -> bool:
    """Partial update, same dynamic-SET pattern as
    update_recurring_commitment. Returns True if a row was matched."""
    sets: list[str] = []
    params: list[Any] = []
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if current_sprint is not None:
        sets.append("current_sprint = ?")
        params.append(current_sprint)
    if sprint_goal is not None:
        sets.append("sprint_goal = ?")
        params.append(sprint_goal)
    if blockers is not None:
        sets.append("blockers = ?")
        params.append(blockers)
    if target_date is not None:
        sets.append("target_date = ?")
        params.append(target_date)
    if run_command is not None:
        sets.append("run_command = ?")
        params.append(run_command)
    if app_url is not None:
        sets.append("app_url = ?")
        params.append(app_url)
    if active is not None:
        sets.append("active = ?")
        params.append(1 if active else 0)
    if not sets:
        return False
    params.append(project_id)
    with session() as conn:
        cursor = conn.execute(
            f"UPDATE registered_projects SET {', '.join(sets)} WHERE id = ?", params
        )
        return cursor.rowcount > 0



def delete_registered_project(project_id: int) -> bool:
    """Hard delete. Linked items keep their full history — only
    project_id is cleared, explicitly in application code rather than
    relying on SQLite's ALTER-added-column FK cascade behavior (which
    is version-dependent for columns added after table creation).

    Any map_nodes card pinning this project (possibly on more than one
    board — see list_pinnable_projects) is deleted outright rather than
    just having project_id cleared: a kind='project' card has no
    stand-alone title/note of its own (list_map_nodes fills those in via
    a live join to registered_projects), so orphaning one would leave a
    permanently blank card with nothing to unpin it. Same
    unlink-dependents-first order as delete_map_node, for anything that
    was pointing at one of those cards as its label parent."""
    with session() as conn:
        conn.execute("UPDATE items SET project_id = NULL WHERE project_id = ?", (project_id,))
        conn.execute(
            "UPDATE map_nodes SET linked_label_id = NULL WHERE linked_label_id IN "
            "(SELECT id FROM map_nodes WHERE project_id = ?)",
            (project_id,),
        )
        conn.execute("DELETE FROM map_nodes WHERE project_id = ?", (project_id,))
        cursor = conn.execute("DELETE FROM registered_projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0
