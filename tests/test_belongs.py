"""What a task belongs to — projects, competitions and /map cards — and
everywhere that shows up: the card breakdown on the map, the card and
competition pages, the purple chips on task cards, the task popup's
Map card picker, and the assistant creating things and linking tasks
to them by name."""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import attachments, auth, belongs, db, queries
from command_center.assistant import ingest as assistant_ingest, tools
from command_center.config import TZ
from command_center.setup_wizard import status as setup_status


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


def _card(kind: str = "research", title: str = "Tubitak RAG", note: str = "", **kwargs) -> int:
    board = queries.get_or_create_default_board()
    return queries.create_map_node(board["id"], kind, title=title, note=note, **kwargs)


# --- linking tasks to cards ---------------------------------------------------


def test_card_lists_its_own_tasks_and_its_projects_and_competitions(isolated_db: None) -> None:
    research = _card("research", "Hallucination study")
    competition_id = queries.create_competition({"title": "Agenthon", "kind": "competition"})
    competition_card = _card("competition", "Agenthon", competition_id=competition_id)
    project_id = queries.create_registered_project("Hukuk Denge", "", "")
    board = queries.get_or_create_default_board()
    project_card = queries.create_map_node(board["id"], "project", project_id=project_id)

    a = queries.create_manual_item(_today(), "action_items", "Read papers")
    queries.set_item_map_node(a, research)
    b = queries.create_manual_item(_today(), "action_items", "Register team")
    queries.set_item_competition(b, competition_id)
    c = queries.create_manual_item(_today(), "action_items", "Ship v1", project_id=project_id)

    assert [i["id"] for i in queries.list_items_for_map_node(research)] == [a]
    assert [i["id"] for i in queries.list_items_for_map_node(competition_card)] == [b]
    assert [i["id"] for i in queries.list_items_for_map_node(project_card)] == [c]


def test_card_task_counts_on_the_board(isolated_db: None) -> None:
    card = _card()
    done = queries.create_manual_item(_today(), "action_items", "Done one")
    queries.set_item_map_node(done, card)
    queries.set_item_status(done, "done")
    pending = queries.create_manual_item(_today(), "action_items", "Open one")
    queries.set_item_map_node(pending, card)

    node = next(n for n in queries.list_map_nodes(queries.get_or_create_default_board()["id"]) if n["id"] == card)
    assert (node["open_tasks"], node["done_tasks"]) == (1, 1)


def test_deleting_a_card_keeps_its_tasks_but_unlinks_them(isolated_db: None) -> None:
    card = _card()
    item_id = queries.create_manual_item(_today(), "action_items", "Draft outline")
    queries.set_item_map_node(item_id, card)

    queries.delete_map_node(card)

    assert queries.get_item(item_id)["map_node_id"] is None


def test_deleting_a_competition_keeps_its_card_but_unlinks_it(isolated_db: None) -> None:
    competition_id = queries.create_competition({"title": "Agenthon"})
    card = _card("competition", "Agenthon", competition_id=competition_id)

    queries.delete_competition(competition_id)

    assert queries.get_map_node(card)["competition_id"] is None


# --- resolving names ------------------------------------------------------------


def test_resolve_matches_exact_then_unique_partial_names(isolated_db: None) -> None:
    _card("research", "Tubitak RAG corporate hallucination")
    queries.create_competition({"title": "Agenthon 2026", "kind": "competition"})

    assert belongs.resolve("agenthon 2026")["type"] == "competition"
    assert belongs.resolve("Agenthon")["name"] == "Agenthon 2026"
    assert belongs.resolve("tubitak rag")["type"] == "card"
    assert belongs.resolve("Something else") is None
    assert belongs.resolve("") is None


def test_resolve_refuses_to_guess_between_two_partial_matches(isolated_db: None) -> None:
    _card("target", "NVIDIA bootcamp")
    _card("target", "NVIDIA certification")
    assert belongs.resolve("nvidia") is None


def test_a_competition_card_is_offered_once_as_the_competition(isolated_db: None) -> None:
    competition_id = queries.create_competition({"title": "Agenthon"})
    _card("competition", "Agenthon", competition_id=competition_id)

    names = [(t["type"], t["name"]) for t in belongs.link_targets()]
    assert names.count(("competition", "Agenthon")) == 1
    assert ("card", "Agenthon") not in names


# --- the map breakdown and the card pages ----------------------------------------


def test_card_detail_is_a_breakdown_with_description_and_tasks(client: TestClient) -> None:
    card = _card("competition", "Agenthon", note="A NeurIPS agent competition.\nFour tracks.", target_date="2026-10-26")
    item_id = queries.create_manual_item(_today(), "action_items", "Pick a track")
    queries.set_item_map_node(item_id, card)

    data = client.get(f"/map/nodes/{card}/detail").json()

    assert data["title"] == "Agenthon"
    assert data["kind_label"] == "Competition"
    assert data["description"] == "A NeurIPS agent competition.\nFour tracks."
    assert data["target_date"] == "2026-10-26"
    assert [t["title"] for t in data["tasks"]] == ["Pick a track"]
    assert data["tasks"][0]["url"] == f"/items/{item_id}/open"
    assert data["page_url"] == f"/map/cards/{card}"


def test_label_has_no_breakdown(client: TestClient) -> None:
    label = _card("label", "October")
    assert client.get(f"/map/nodes/{label}/detail").status_code == 400


def test_adding_a_task_from_a_card_links_it_there(client: TestClient) -> None:
    card = _card()
    response = client.post(f"/map/nodes/{card}/tasks", json={"title": "Collect datasets"})
    assert response.status_code == 200
    item = queries.get_item(response.json()["id"])
    assert item["map_node_id"] == card
    assert item["brief_date"] == _today()


def test_adding_a_task_from_a_competition_card_links_the_competition(client: TestClient) -> None:
    competition_id = queries.create_competition({"title": "Agenthon"})
    card = _card("competition", "Agenthon", competition_id=competition_id)
    item_id = client.post(f"/map/nodes/{card}/tasks", json={"title": "Read rules"}).json()["id"]
    item = queries.get_item(item_id)
    assert item["competition_id"] == competition_id
    assert item["map_node_id"] is None


def test_card_page_shows_the_full_description_and_tasks(client: TestClient) -> None:
    card = _card("research", "Hallucination study", note="Measure hallucination in corporate RAG.")
    item_id = queries.create_manual_item(_today(), "action_items", "Build eval set")
    queries.set_item_map_node(item_id, card)

    response = client.get(f"/map/cards/{card}")

    assert response.status_code == 200
    assert "Measure hallucination in corporate RAG." in response.text
    assert "Build eval set" in response.text


def test_project_card_page_is_the_project_page(client: TestClient) -> None:
    project_id = queries.create_registered_project("Hukuk Denge", "", "")
    board = queries.get_or_create_default_board()
    card = queries.create_map_node(board["id"], "project", project_id=project_id)
    response = client.get(f"/map/cards/{card}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"/projects/{project_id}"


def test_folderless_project_page_renders_without_scanning_anything(client: TestClient) -> None:
    project_id = queries.create_registered_project("Idea only", "", "Just an idea")
    response = client.get(f"/projects/{project_id}")
    assert response.status_code == 200
    assert "No folder linked yet" in response.text


def test_competition_page_shows_details_and_tasks(client: TestClient) -> None:
    competition_id = client.post(
        "/competitions", json={"title": "ETH Global", "prize": "$10k", "notes": "Build on Ethereum"}
    ).json()["id"]
    client.post(f"/competitions/{competition_id}/tasks", json={"title": "Find teammates"})

    html = client.get(f"/competitions/{competition_id}").text

    assert "ETH Global" in html and "$10k" in html and "Build on Ethereum" in html
    assert "Find teammates" in html


def test_send_to_map_links_the_card_and_never_duplicates_it(client: TestClient) -> None:
    competition_id = client.post("/competitions", json={"title": "ETH Global"}).json()["id"]
    first = client.post(f"/competitions/{competition_id}/to-map").json()
    second = client.post(f"/competitions/{competition_id}/to-map").json()
    assert first["node_id"] == second["node_id"]
    assert queries.get_map_node(first["node_id"])["competition_id"] == competition_id


def test_map_link_to_a_card_lands_on_that_cards_board(client: TestClient) -> None:
    queries.get_or_create_default_board()
    november = queries.create_map_board("November")
    card = queries.create_map_node(november, "research", title="On the second board")

    html = client.get(f"/map?card={card}").text

    assert f", {november})'" in html  # mindMapCanvas(..., board_id) is November's
    assert "openFromUrl" in html


# --- chips and the task popup ------------------------------------------------------


def test_task_card_shows_a_purple_chip_to_its_card_page(client: TestClient) -> None:
    card = _card("research", "Hallucination study")
    item_id = queries.create_manual_item(_today(), "action_items", "Build eval set")
    queries.set_item_map_node(item_id, card)

    html = client.get("/brief").text

    assert "belongs-chip" in html
    assert f'href="/map/cards/{card}"' in html
    assert "Hallucination study" in html


def test_linking_a_task_to_a_card_from_the_popup(client: TestClient) -> None:
    card = _card()
    item_id = queries.create_manual_item(_today(), "action_items", "Read papers")

    assert client.patch(f"/items/{item_id}/map-node", json={"map_node_id": card}).status_code == 200
    assert client.get(f"/items/{item_id}").json()["map_node_title"] == "Tubitak RAG"
    assert client.patch(f"/items/{item_id}/map-node", json={"map_node_id": None}).status_code == 200
    assert queries.get_item(item_id)["map_node_id"] is None


def test_a_task_cant_be_linked_to_a_label(client: TestClient) -> None:
    label = _card("label", "October")
    item_id = queries.create_manual_item(_today(), "action_items", "Read papers")
    assert client.patch(f"/items/{item_id}/map-node", json={"map_node_id": label}).status_code == 400


# --- the assistant -------------------------------------------------------------------


def test_validate_creator_args(isolated_db: None) -> None:
    assert tools.validate_args("create_map_card", {"kind": "label", "title": "x"}) is not None
    assert tools.validate_args("create_map_card", {"kind": "research", "title": " "}) is not None
    assert tools.validate_args("create_competition", {"title": "x", "deadline": "soon"}) is not None
    assert tools.validate_args("create_project", {}) is not None


def test_confirmation_text_says_what_a_task_will_be_linked_to(isolated_db: None) -> None:
    _card("research", "Hallucination study")
    linked = tools.describe_pending("create_task", {"title": "Read", "belongs_to": "hallucination study"}, {})
    assert "for Hallucination study (Research)" in linked
    new = tools.describe_pending("create_task", {"title": "Read", "belongs_to": "Agenthon"}, {}, new_names={"agenthon"})
    assert "for Agenthon (new)" in new
    missing = tools.describe_pending("create_task", {"title": "Read", "belongs_to": "Nope"}, {})
    assert "won't be linked" in missing


# --- saving a Map competition card on the Competitions page -----------------------


def test_competitions_page_offers_competition_cards_made_on_the_map(client: TestClient) -> None:
    _card("competition", "Agenthon", note="NeurIPS agents")
    _card("research", "Not a competition")
    saved_id = queries.create_competition({"title": "Already saved"})
    _card("hackathon", "Already saved", competition_id=saved_id)

    titles = [c["title"] for c in queries.list_unlinked_competition_cards()]
    assert titles == ["Agenthon"]
    assert "Already on your Map" in client.get("/competitions").text


def test_saving_from_a_map_card_links_that_card_instead_of_duplicating(client: TestClient) -> None:
    card = _card("competition", "Agenthon", note="NeurIPS agents")
    item_id = queries.create_manual_item(_today(), "action_items", "Pick a track")
    queries.set_item_map_node(item_id, card)

    response = client.post("/competitions", json={"title": "Agenthon", "from_card_id": card})

    assert response.status_code == 200, response.text
    competition_id = response.json()["id"]
    assert queries.get_map_node(card)["competition_id"] == competition_id
    assert queries.list_unlinked_competition_cards() == []
    # Sending it to the map afterwards reuses the card, and the card's
    # existing tasks show on the competition's page.
    assert client.post(f"/competitions/{competition_id}/to-map").json()["node_id"] == card
    assert "Pick a track" in client.get(f"/competitions/{competition_id}").text


def test_saving_from_a_stale_card_pick_creates_nothing(client: TestClient) -> None:
    research = _card("research", "Not a competition")
    response = client.post("/competitions", json={"title": "X", "from_card_id": research})
    assert response.status_code == 400
    assert queries.list_competitions() == []


# --- where "Open" leads ---------------------------------------------------------------


def test_saved_competition_card_opens_in_the_competitions_list(client: TestClient) -> None:
    competition_id = queries.create_competition({"title": "Agenthon", "kind": "hackathon"})
    card = _card("hackathon", "Agenthon", competition_id=competition_id)

    data = client.get(f"/map/nodes/{card}/detail").json()

    assert data["open_url"] == f"/competitions?open={competition_id}"
    assert data["open_label"] == "Open in Competitions"
    assert data["page_url"] == f"/competitions/{competition_id}"  # "Full details" from the list
    assert data["save_url"] is None


def test_map_only_competition_card_offers_saving_it_to_competitions(client: TestClient) -> None:
    card = _card("competition", "Agenthon")
    data = client.get(f"/map/nodes/{card}/detail").json()
    assert data["save_url"] == f"/competitions?adopt={card}"
    assert data["open_url"] == f"/map/cards/{card}"


def test_research_card_opens_its_own_page(client: TestClient) -> None:
    card = _card("research", "Hallucination study")
    data = client.get(f"/map/nodes/{card}/detail").json()
    assert (data["open_url"], data["save_url"]) == (f"/map/cards/{card}", None)


def test_competition_full_page_links_back_to_its_spot_in_the_list(client: TestClient) -> None:
    competition_id = queries.create_competition({"title": "Agenthon"})
    assert f'href="/competitions?open={competition_id}"' in client.get(f"/competitions/{competition_id}").text
