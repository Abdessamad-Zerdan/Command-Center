"""The Pursuits section (Research / Targets pages, the tab row, Map cards
standing for entries) and the date-and-time labels on cards."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import attachments, auth, belongs, db, queries, timestamps
from command_center.assistant import ingest as assistant_ingest
from command_center.config import TZ
from command_center.setup_wizard import status as setup_status
from command_center.sources import to_local_iso


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", tmp_path / "attachments")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


# --- Map cards stand for Research / Targets entries --------------------------------


def test_existing_research_and_target_cards_are_adopted_with_their_tasks(isolated_db: None) -> None:
    board = queries.get_or_create_default_board()
    research = queries.create_map_node(board["id"], "research", title="RAG hallucination", note="Corporate docs")
    target = queries.create_map_node(board["id"], "target", title="NVIDIA bootcamp", target_date="2026-11-01")
    hackathon = queries.create_map_node(board["id"], "hackathon", title="Not adopted")
    item_id = queries.create_manual_item(_today(), "action_items", "Read papers")
    queries.set_item_map_node(item_id, research)

    db.init_db()  # what a restart does
    db.init_db()  # and doing it twice changes nothing

    [r] = queries.list_pursuits("research")
    [t] = queries.list_pursuits("target")
    assert (r["title"], r["notes"], r["status"], r["map_node_id"]) == ("RAG hallucination", "Corporate docs", "idea", research)
    assert (t["status"], t["target_date"]) == ("planned", "2026-11-01")
    assert queries.get_map_node(hackathon)["pursuit_id"] is None
    item = queries.get_item(item_id)
    assert (item["pursuit_id"], item["map_node_id"]) == (r["id"], None)


def test_adding_a_research_card_on_the_map_creates_its_entry(client: TestClient) -> None:
    board = queries.get_or_create_default_board()
    node_id = client.post(
        "/map/nodes", json={"board_id": board["id"], "kind": "research", "title": "Agents", "note": "Survey"}
    ).json()["id"]

    [entry] = queries.list_pursuits("research")
    assert entry["title"] == "Agents" and entry["map_node_id"] == node_id


def test_editing_either_side_edits_both(client: TestClient) -> None:
    board = queries.get_or_create_default_board()
    node_id = client.post("/map/nodes", json={"board_id": board["id"], "kind": "target", "title": "Bootcamp"}).json()["id"]
    pursuit_id = queries.get_map_node(node_id)["pursuit_id"]

    client.patch(f"/map/nodes/{node_id}", json={"title": "NVIDIA bootcamp", "note": "Week 1", "target_date": "2026-11-01"})
    assert queries.get_pursuit(pursuit_id)["title"] == "NVIDIA bootcamp"

    client.patch(f"/pursuits/{pursuit_id}", json={"title": "NVIDIA DLI bootcamp", "notes": "Week 2"})
    node = queries.get_map_node(node_id)
    assert (node["title"], node["note"]) == ("NVIDIA DLI bootcamp", "Week 2")


def test_deleting_an_entry_removes_its_map_card_but_keeps_tasks(client: TestClient) -> None:
    pursuit_id = client.post("/pursuits", json={"kind": "research", "title": "Agents"}).json()["id"]
    node_id = client.post(f"/pursuits/{pursuit_id}/to-map").json()["node_id"]
    task_id = client.post(f"/pursuits/{pursuit_id}/tasks", json={"title": "Read survey"}).json()["id"]

    assert client.delete(f"/pursuits/{pursuit_id}").status_code == 200

    assert queries.get_map_node(node_id) is None
    assert queries.get_item(task_id)["pursuit_id"] is None


# --- the Research / Targets pages ----------------------------------------------------


def test_research_page_lists_entries_with_open_tasks(client: TestClient) -> None:
    pursuit_id = client.post("/pursuits", json={"kind": "research", "title": "RAG evals", "notes": "Notes"}).json()["id"]
    client.post(f"/pursuits/{pursuit_id}/tasks", json={"title": "Build eval set"})

    html = client.get("/research").text

    assert "pursuitsBoard(" in html and "RAG evals" in html
    assert '"open_tasks": 1' in html
    assert queries.list_pursuits("target") == []


def test_statuses_are_per_page(client: TestClient) -> None:
    research_id = client.post("/pursuits", json={"kind": "research", "title": "A"}).json()["id"]
    target_id = client.post("/pursuits", json={"kind": "target", "title": "B"}).json()["id"]

    assert client.patch(f"/pursuits/{research_id}", json={"status": "active"}).status_code == 200
    assert client.patch(f"/pursuits/{target_id}", json={"status": "in_progress"}).status_code == 200
    assert client.patch(f"/pursuits/{target_id}", json={"status": "done"}).status_code == 400
    assert client.post("/pursuits", json={"kind": "label", "title": "x"}).status_code == 400


def test_full_details_page_and_wrong_kind_redirect(client: TestClient) -> None:
    pursuit_id = client.post("/pursuits", json={"kind": "target", "title": "Certificate"}).json()["id"]
    assert "Certificate" in client.get(f"/targets/{pursuit_id}").text
    wrong = client.get(f"/research/{pursuit_id}", follow_redirects=False)
    assert wrong.headers["location"] == f"/targets/{pursuit_id}"


def test_map_card_for_an_entry_opens_on_its_page(client: TestClient) -> None:
    pursuit_id = client.post("/pursuits", json={"kind": "research", "title": "Agents"}).json()["id"]
    node_id = client.post(f"/pursuits/{pursuit_id}/to-map").json()["node_id"]

    data = client.get(f"/map/nodes/{node_id}/detail").json()

    assert data["open_url"] == f"/research?open={pursuit_id}"
    assert data["open_label"] == "Open in Research"
    assert data["page_url"] == f"/research/{pursuit_id}"


def test_task_for_an_entry_gets_a_chip_and_is_linkable_by_name(client: TestClient) -> None:
    pursuit_id = client.post("/pursuits", json={"kind": "target", "title": "NVIDIA bootcamp"}).json()["id"]
    item_id = queries.create_manual_item(_today(), "action_items", "Register")
    queries.set_item_pursuit(item_id, pursuit_id)

    html = client.get("/brief").text

    assert f'href="/targets?open={pursuit_id}"' in html
    target = belongs.resolve("nvidia bootcamp")
    assert (target["type"], target["id"]) == ("pursuit", pursuit_id)


# --- the Pursuits section ------------------------------------------------------------


@pytest.mark.parametrize("path", ["/map", "/competitions", "/research", "/targets", "/projects"])
def test_every_pursuits_page_has_the_tab_row(client: TestClient, path: str) -> None:
    html = client.get(path).text
    assert 'aria-label="Pursuits"' in html
    for href in ("/map", "/competitions", "/research", "/targets", "/projects"):
        assert f'href="{href}"' in html
    assert html.count('aria-current="page"') == 1


def test_tab_counts_only_count_what_is_still_in_play(isolated_db: None) -> None:
    queries.create_pursuit("research", {"title": "Open"})
    queries.create_pursuit("research", {"title": "Finished", "status": "done"})
    queries.create_competition({"title": "Passed", "status": "passed"})
    assert queries.pursuits_tab_counts() == {"competitions": 0, "research": 1, "targets": 0, "projects": 0}


def test_projects_grid_and_project_tasks(client: TestClient, tmp_path: Path) -> None:
    project_id = queries.create_registered_project("Hukuk Denge", str(tmp_path), "Legal AI")
    task = client.post(f"/projects/{project_id}/tasks", json={"title": "Ship v1"}).json()

    assert queries.get_item(task["id"])["project_id"] == project_id
    html = client.get("/projects").text
    assert "projectsBoard(" in html and '"open_tasks": 1' in html
    node_id = client.post(f"/projects/{project_id}/to-map").json()["node_id"]
    assert client.post(f"/projects/{project_id}/to-map").json()["node_id"] == node_id


# --- date-and-time labels ---------------------------------------------------------------


def test_gmail_internal_date_becomes_a_local_time() -> None:
    iso = to_local_iso("1791800000000")  # epoch ms, Gmail's internalDate
    assert datetime.fromisoformat(iso).timestamp() == 1791800000
    assert datetime.fromisoformat(iso).tzinfo is not None
    assert to_local_iso("2026-10-12T09:30:00.000Z") is not None
    assert to_local_iso("") is None and to_local_iso("garbage") is None


def test_stamp_format_and_meaning() -> None:
    now = datetime.now(TZ).replace(hour=14, minute=5, second=0, microsecond=0)
    assert timestamps.stamp(now.isoformat()).endswith("14:05")
    last_year = (now - timedelta(days=400)).isoformat()
    assert str((now - timedelta(days=400)).year) in timestamps.stamp(last_year)

    mail = {"source": "gmail", "source_time": now.isoformat(), "created_at": (now + timedelta(hours=3)).isoformat()}
    assert timestamps.item_stamp(mail)["label"] == "Received"
    assert timestamps.item_stamp(mail)["iso"] == now.isoformat()
    manual = {"source": "manual", "source_time": None, "created_at": now.isoformat()}
    assert timestamps.item_stamp(manual)["label"] == "Added"
    assert timestamps.item_stamp({"source": "manual", "created_at": None}) is None


def test_task_cards_show_when_they_were_added(client: TestClient) -> None:
    queries.create_manual_item(_today(), "action_items", "Call the bank")
    html = client.get("/brief").text
    assert "Added " in html and "<time datetime=" in html


def test_a_mail_keeps_its_arrival_time_and_existing_ones_get_it_filled_in(isolated_db: None) -> None:
    item = {
        "lane": "urgent", "source": "gmail", "source_id": "m1", "title": "Invoice", "why_it_matters": "",
        "suggested_next_step": "", "priority": 1, "deep_link": "", "due_date": None,
    }
    queries.save_triage_results(_today(), [item], [], [], ["gmail"])
    assert _items_by_source_id("m1")["source_time"] is None  # saved before times were kept

    arrived = "2026-10-12T08:15:00+03:00"
    queries.save_triage_results(_today(), [{**item, "source_time": arrived}], [], [], ["gmail"])
    assert _items_by_source_id("m1")["source_time"] == arrived


def _items_by_source_id(source_id: str) -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM items WHERE source_id = ?", (source_id,)).fetchone()
        return dict(row) if row else None
