"""What a task "belongs to" — a project, a saved competition, a research
topic or target (pursuits), or a bare /map card — and where each of those
lives.

One place for this so the task chips, the pages, the task popup and the
assistant all agree: a task can be linked through items.project_id,
items.competition_id, items.pursuit_id or items.map_node_id, and a map
card that stands for a project/competition/pursuit shares that one's
tasks instead of keeping its own (see queries._NODE_TASKS_WHERE).

Every thing has a home in the Pursuits section — its page's list, with
its popup open (`*_in_list_url`) — and a full-details page behind that.
"""

from typing import Any

from command_center import queries, timestamps

# Display names for a target's kind — map card kinds plus competition kinds.
KIND_LABELS = {
    "project": "Project",
    "competition": "Competition",
    "hackathon": "Hackathon",
    "fellowship": "Fellowship",
    "research": "Research",
    "target": "Target",
    "other": "Other",
}

# /research and /targets — the page each pursuit kind lives on.
PURSUIT_PAGES = {"research": "research", "target": "targets"}
PURSUIT_PAGE_TITLES = {"research": "Research", "target": "Targets"}


def card_url(node_id: int) -> str:
    return f"/map/cards/{node_id}"


def competition_url(competition_id: int) -> str:
    return f"/competitions/{competition_id}"


def competition_in_list_url(competition_id: int) -> str:
    """The competition's home: the Competitions list, scrolled to it with
    its popup open (whose "Full details" leads on to competition_url)."""
    return f"/competitions?open={competition_id}"


def project_url(project_id: int) -> str:
    return f"/projects/{project_id}"


def project_in_list_url(project_id: int) -> str:
    return f"/projects?open={project_id}"


def pursuit_url(pursuit: dict[str, Any]) -> str:
    return f"/{PURSUIT_PAGES[pursuit['kind']]}/{pursuit['id']}"


def pursuit_in_list_url(pursuit: dict[str, Any]) -> str:
    return f"/{PURSUIT_PAGES[pursuit['kind']]}?open={pursuit['id']}"


def link_fields_for_node(node: dict[str, Any]) -> dict[str, int]:
    """The items column(s) a new task for this card should set. A card
    that stands for a project/competition/pursuit links the task to that
    (so its page lists it too); any other card owns the task directly."""
    if node.get("project_id"):
        return {"project_id": node["project_id"]}
    if node.get("competition_id"):
        return {"competition_id": node["competition_id"]}
    if node.get("pursuit_id"):
        return {"pursuit_id": node["pursuit_id"]}
    return {"map_node_id": node["id"]}


def link_targets() -> list[dict[str, Any]]:
    """Everything a task can belong to, as {type, id, name, kind, url}.
    A card that stands for a competition or pursuit is left out — that
    thing itself is the target, so it isn't offered twice."""
    targets = [
        {"type": "project", "id": p["id"], "name": p["name"], "kind": "project", "url": project_in_list_url(p["id"])}
        for p in queries.list_registered_projects(active_only=True)
    ]
    targets += [
        {"type": "competition", "id": c["id"], "name": c["title"], "kind": c["kind"],
         "url": competition_in_list_url(c["id"])}
        for c in queries.list_competitions()
        if c["status"] != "passed"
    ]
    for kind in queries.PURSUIT_KINDS:
        finished = queries.PURSUIT_STATUSES[kind][-1]
        targets += [
            {"type": "pursuit", "id": p["id"], "name": p["title"], "kind": kind, "url": pursuit_in_list_url(p)}
            for p in queries.list_pursuits(kind)
            if p["status"] != finished
        ]
    targets += [
        {"type": "card", "id": n["id"], "name": n["title"], "kind": n["kind"], "url": card_url(n["id"])}
        for n in queries.list_linkable_map_cards()
        if not n["competition_id"] and not n["pursuit_id"]
    ]
    return targets


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def resolve(name: str | None, kind: str | None = None, targets: list[dict] | None = None) -> dict | None:
    """Finds the target a name (as the user or the assistant wrote it)
    refers to: an exact, case-insensitive match first, then a unique
    partial match ("agenthon" → "Agenthon 2026"). Ambiguous or unknown →
    None, never a guess. `kind` narrows by type/kind when given."""
    if not name or not str(name).strip():
        return None
    wanted = _norm(name)
    pool = targets if targets is not None else link_targets()
    if kind:
        narrowed = [t for t in pool if kind in (t["type"], t["kind"])]
        pool = narrowed or pool
    exact = [t for t in pool if _norm(t["name"]) == wanted]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    partial = [t for t in pool if wanted in _norm(t["name"]) or _norm(t["name"]) in wanted]
    return partial[0] if len(partial) == 1 else None


_TARGET_COLUMNS = {
    "project": "project_id", "competition": "competition_id", "pursuit": "pursuit_id", "card": "map_node_id",
}


def item_fields_for_target(target: dict[str, Any]) -> dict[str, int]:
    return {_TARGET_COLUMNS[target["type"]]: target["id"]}


def apply_to_item(item_id: int, fields: dict[str, int]) -> None:
    if "project_id" in fields:
        queries.update_item_project(item_id, fields["project_id"])
    if "competition_id" in fields:
        queries.set_item_competition(item_id, fields["competition_id"])
    if "pursuit_id" in fields:
        queries.set_item_pursuit(item_id, fields["pursuit_id"])
    if "map_node_id" in fields:
        queries.set_item_map_node(item_id, fields["map_node_id"])


class Lookups:
    """Names for everything a task can belong to, loaded once per page
    rather than once per task card."""

    def __init__(self) -> None:
        self.projects = {p["id"]: p["name"] for p in queries.list_registered_projects()}
        self.competitions = {c["id"]: c for c in queries.list_competitions()}
        self.pursuits = {p["id"]: p for kind in queries.PURSUIT_KINDS for p in queries.list_pursuits(kind)}
        self.cards = {n["id"]: n for n in queries.list_linkable_map_cards()}

    def chips(self, item: dict[str, Any]) -> list[dict[str, str]]:
        """The "belongs to" chips for one task, as {name, kind, label, url}."""
        chips = []
        project_id = item.get("project_id")
        if project_id and project_id in self.projects:
            chips.append({
                "name": self.projects[project_id], "kind": "project", "label": "Project",
                "url": project_in_list_url(project_id),
            })
        competition = self.competitions.get(item.get("competition_id"))
        if competition:
            chips.append({
                "name": competition["title"], "kind": competition["kind"],
                "label": KIND_LABELS.get(competition["kind"], "Competition"),
                "url": competition_in_list_url(competition["id"]),
            })
        pursuit = self.pursuits.get(item.get("pursuit_id"))
        if pursuit:
            chips.append({
                "name": pursuit["title"], "kind": pursuit["kind"], "label": KIND_LABELS[pursuit["kind"]],
                "url": pursuit_in_list_url(pursuit),
            })
        card = self.cards.get(item.get("map_node_id"))
        if card:
            chips.append({
                "name": card["title"], "kind": card["kind"],
                "label": KIND_LABELS.get(card["kind"], card["kind"].title()), "url": card_url(card["id"]),
            })
        return chips


def annotate(items: list[dict[str, Any]]) -> None:
    """Mutates each item in place with `belongs_to` chips (plus the
    project_name/competition_title/pursuit_title/map_node_title fields
    the task popup reads)."""
    if not any(
        i.get("project_id") or i.get("competition_id") or i.get("pursuit_id") or i.get("map_node_id") for i in items
    ):
        for item in items:
            item["belongs_to"] = []
        return
    lookups = Lookups()
    for item in items:
        item["belongs_to"] = lookups.chips(item)
        item["project_name"] = lookups.projects.get(item.get("project_id"))
        competition = lookups.competitions.get(item.get("competition_id"))
        item["competition_title"] = competition["title"] if competition else None
        pursuit = lookups.pursuits.get(item.get("pursuit_id"))
        item["pursuit_title"] = pursuit["title"] if pursuit else None
        card = lookups.cards.get(item.get("map_node_id"))
        item["map_node_title"] = card["title"] if card else None


def task_json(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "title": item["title"],
        "status": item["status"],
        "due_date": item["due_date"],
        "brief_date": item["brief_date"],
        "url": f"/items/{item['id']}/open",
        # {label, iso, text, full} — "Received 13 Oct, 14:05" etc.
        "stamp": timestamps.item_stamp(item),
    }


def _competition_facts(c: dict[str, Any]) -> list[dict[str, str]]:
    facts = []
    for key, label in (
        ("organizer", "Organizer"), ("event_dates", "Event dates"), ("location", "Where"),
        ("prize", "Prize"), ("status", "Status"),
    ):
        if c.get(key):
            facts.append({"label": label, "value": str(c[key]).capitalize() if key == "status" else c[key]})
    return facts


def status_label(status: str) -> str:
    return status.replace("_", " ").capitalize()


def _resolve_focus_records(
    node: dict[str, Any] | None,
    competition: dict[str, Any] | None,
    pursuit: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Fills in whichever side is missing — a card's competition/pursuit,
    or the card that stands for a competition/pursuit — plus the card's
    registered project. Returns (node, competition, pursuit, project)."""
    if node is not None and competition is None and node.get("competition_id"):
        competition = queries.get_competition(node["competition_id"])
    if node is not None and pursuit is None and node.get("pursuit_id"):
        pursuit = queries.get_pursuit(node["pursuit_id"])
    if competition is not None and node is None:
        node = queries.find_map_node_for_competition(competition["id"])
    if pursuit is not None and node is None:
        node = queries.find_map_node_for_pursuit(pursuit["id"])

    project = None
    if node is not None and node.get("project_id"):
        project = queries.get_registered_project(node["project_id"])
    return node, competition, pursuit, project


def _focus_items(node, competition, pursuit) -> list[dict[str, Any]]:
    if node is not None:
        return queries.list_items_for_map_node(node["id"])
    if competition is not None:
        return queries.list_items_by_competition(competition["id"])
    if pursuit is not None:
        return queries.list_items_by_pursuit(pursuit["id"])
    return []


def _focus_content(node, competition, pursuit, project) -> dict[str, Any]:
    """kind, title, description, target_date and facts — taken from the
    most specific record: project, then competition, then pursuit, then
    the bare card."""
    facts: list[dict[str, str]] = []
    if project is not None:
        kind, title = "project", project["name"]
        description = "\n\n".join(
            part for part in (project.get("description"), project.get("current_sprint") and f"Current sprint: {project['current_sprint']}",
                              project.get("sprint_goal") and f"Goal: {project['sprint_goal']}",
                              project.get("blockers") and f"Blockers: {project['blockers']}") if part
        )
        target_date = project.get("target_date")
        if project.get("path"):
            facts.append({"label": "Folder", "value": project["path"]})
    elif competition is not None:
        kind, title = competition["kind"], competition["title"]
        # The card's own note is usually the richer write-up (it's what
        # the user typed on the map); the competition's notes add to it.
        # Send to Map writes "organizer · prize · dates" as the card note —
        # already in the facts below, so not repeated as a description.
        summary = " · ".join(d for d in (competition.get("organizer"), competition.get("prize"), competition.get("event_dates")) if d)
        card_note = node.get("note") if node else None
        parts = [card_note if card_note != summary else None, competition.get("notes")]
        description = "\n\n".join(p for p in dict.fromkeys(parts) if p)
        target_date = competition.get("deadline") or (node.get("target_date") if node else None)
        facts += _competition_facts(competition)
    elif pursuit is not None:
        kind, title = pursuit["kind"], pursuit["title"]
        description = pursuit.get("notes") or ""
        target_date = pursuit.get("target_date")
        facts.append({"label": "Status", "value": status_label(pursuit["status"])})
    else:
        kind, title = node["kind"], node["title"]
        description = node.get("note") or ""
        target_date = node.get("target_date")
    return {"kind": kind, "title": title, "description": description, "target_date": target_date, "facts": facts}


def _focus_links(node, competition, pursuit, project) -> dict[str, Any]:
    """open_url/open_label: where a card's "Open" leads — each thing's home
    in the Pursuits section. A competition card that only exists on the
    Map has no home there yet, so it offers save_url (the Competitions
    composer, adopting this card)."""
    save_url = None
    node_tasks_url = f"/map/nodes/{node['id']}/tasks" if node is not None else None
    if project is not None:
        page_url, add_task_url = project_url(project["id"]), node_tasks_url
        edit_url = project_in_list_url(project["id"])
        open_url, open_label = edit_url, "Open in Projects"
        back = {"label": "Projects", "url": edit_url}
    elif competition is not None:
        page_url = competition_url(competition["id"])
        add_task_url = node_tasks_url or f"/competitions/{competition['id']}/tasks"
        edit_url = competition_in_list_url(competition["id"])
        open_url, open_label = edit_url, "Open in Competitions"
        back = {"label": "Competitions", "url": edit_url}
    elif pursuit is not None:
        page_url = pursuit_url(pursuit)
        add_task_url = node_tasks_url or f"/pursuits/{pursuit['id']}/tasks"
        edit_url = pursuit_in_list_url(pursuit)
        open_url, open_label = edit_url, f"Open in {PURSUIT_PAGE_TITLES[pursuit['kind']]}"
        back = {"label": PURSUIT_PAGE_TITLES[pursuit["kind"]], "url": edit_url}
    else:
        page_url, add_task_url = card_url(node["id"]), node_tasks_url
        edit_url = f"/map?board={node['board_id']}&card={node['id']}&edit=1"
        open_url, open_label = page_url, "Open full page"
        back = None
        if node["kind"] in ("competition", "hackathon"):
            save_url = f"/competitions?adopt={node['id']}"
    return {
        "back": back,
        "page_url": page_url,
        "open_url": open_url,
        "open_label": open_label,
        "save_url": save_url,
        "edit_url": edit_url,
        "add_task_url": add_task_url,
    }


def _focus_image_url(competition, pursuit) -> str | None:
    if competition and competition.get("screenshot_name"):
        return f"/competitions/{competition['id']}/screenshot"
    if pursuit and pursuit.get("cover_name"):
        return f"/pursuits/{pursuit['id']}/cover"
    return None


def focus_view(
    node: dict[str, Any] | None = None,
    competition: dict[str, Any] | None = None,
    pursuit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything the breakdown of one "thing" shows — a map card, a saved
    competition, a research topic / target, or a card that stands for one
    — as plain JSON. Feeds both the /map card popup and the full-details
    pages, so the two can never disagree."""
    node, competition, pursuit, project = _resolve_focus_records(node, competition, pursuit)
    content = _focus_content(node, competition, pursuit, project)
    links = _focus_links(node, competition, pursuit, project)
    board = queries.get_map_board(node["board_id"]) if node is not None else None
    label = queries.get_map_node(node["linked_label_id"]) if node is not None and node.get("linked_label_id") else None
    tasks = [task_json(i) for i in _focus_items(node, competition, pursuit)]
    kind = content["kind"]
    return {
        "node_id": node["id"] if node is not None else None,
        "competition_id": competition["id"] if competition is not None else None,
        "pursuit_id": pursuit["id"] if pursuit is not None else None,
        "project_id": project["id"] if project is not None else None,
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind.title()),
        "title": content["title"],
        # When it was added — the thing's own record if it has one (its
        # page's entry), otherwise the Map card's.
        "created_at": (project or competition or pursuit or node or {}).get("created_at"),
        "description": content["description"],
        "target_date": content["target_date"],
        "url": (competition or pursuit or {}).get("url"),
        "facts": content["facts"],
        "board": {"id": board["id"], "name": board["name"]} if board else None,
        "label": {"id": label["id"], "title": label["title"]} if label else None,
        "back": links["back"],
        "tasks": tasks,
        "open_count": sum(1 for t in tasks if t["status"] == "pending"),
        "done_count": sum(1 for t in tasks if t["status"] == "done"),
        "page_url": links["page_url"],
        "open_url": links["open_url"],
        "open_label": links["open_label"],
        "save_url": links["save_url"],
        "edit_url": links["edit_url"],
        "add_task_url": links["add_task_url"],
        "image_url": _focus_image_url(competition, pursuit),
    }
