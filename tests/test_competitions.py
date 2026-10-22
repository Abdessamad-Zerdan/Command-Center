from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import attachments, auth, db, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.setup_wizard import status as setup_status

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture()
def photo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "attachments"
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", target)
    return target


@pytest.fixture()
def client(tmp_path: Path, photo_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def _create(client: TestClient, **fields) -> dict:
    payload = {"title": "NASA Space Apps", **fields}
    response = client.post("/competitions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_create_defaults_and_round_trip(client: TestClient) -> None:
    created = _create(client, deadline="2026-11-14", prize="  Trip to HQ  ", notes="")

    assert created["kind"] == "hackathon"
    assert created["status"] == "saved"
    assert created["prize"] == "Trip to HQ"  # trimmed
    assert created["notes"] is None  # blank becomes empty, not ""
    assert created["screenshot_url"] is None


def test_create_requires_a_name(client: TestClient) -> None:
    assert client.post("/competitions", json={"title": "   "}).status_code == 400


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,<b>x</b>", "file:///etc/passwd"])
def test_links_must_be_web_addresses(client: TestClient, url: str) -> None:
    # The link is rendered as a clickable href — no other scheme gets in.
    response = client.post("/competitions", json={"title": "X", "url": url})
    assert response.status_code == 400


def test_a_bare_domain_link_is_assumed_https(client: TestClient) -> None:
    created = _create(client, url="devpost.com/hackathons")
    assert created["url"] == "https://devpost.com/hackathons"


@pytest.mark.parametrize(
    "fields",
    [{"kind": "party"}, {"status": "maybe"}, {"deadline": "next friday"}, {"title": "x" * 201}],
)
def test_invalid_fields_are_rejected(client: TestClient, fields: dict) -> None:
    assert client.post("/competitions", json={"title": "X", **fields}).status_code == 400


def test_patch_updates_only_what_was_sent(client: TestClient) -> None:
    created = _create(client, organizer="NASA", deadline="2026-11-14")

    updated = client.patch(f"/competitions/{created['id']}", json={"status": "shortlisted"}).json()

    assert updated["status"] == "shortlisted"
    assert updated["organizer"] == "NASA"
    assert updated["deadline"] == "2026-11-14"


def test_patch_and_delete_404_for_unknown(client: TestClient) -> None:
    assert client.patch("/competitions/999", json={"status": "saved"}).status_code == 404
    assert client.delete("/competitions/999").status_code == 404


def test_screenshot_upload_serve_replace_and_cleanup(client: TestClient, photo_dir: Path) -> None:
    created = _create(client)
    cid = created["id"]

    first = client.post(f"/competitions/{cid}/screenshot", files={"file": ("shot.png", PNG, "image/png")}).json()
    served = client.get(first["screenshot_url"])
    assert served.status_code == 200
    assert served.content == PNG
    assert served.headers["x-content-type-options"] == "nosniff"

    second = client.post(f"/competitions/{cid}/screenshot", files={"file": ("shot.jpg", JPEG, "image/jpeg")}).json()
    assert second["screenshot_url"] != first["screenshot_url"]  # cache-busted
    assert client.get(second["screenshot_url"]).headers["content-type"] == "image/jpeg"
    assert len(list(photo_dir.iterdir())) == 1  # the replaced file is gone

    client.delete(f"/competitions/{cid}")
    assert not any(photo_dir.iterdir())


def test_screenshot_rejects_non_images(client: TestClient) -> None:
    created = _create(client)
    response = client.post(
        f"/competitions/{created['id']}/screenshot",
        files={"file": ("x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")},
    )
    assert response.status_code == 400


def test_removing_a_screenshot(client: TestClient, photo_dir: Path) -> None:
    created = _create(client)
    client.post(f"/competitions/{created['id']}/screenshot", files={"file": ("shot.png", PNG, "image/png")})

    cleared = client.delete(f"/competitions/{created['id']}/screenshot").json()

    assert cleared["screenshot_url"] is None
    assert not any(photo_dir.iterdir())


def test_send_to_map_creates_a_matching_card(client: TestClient) -> None:
    created = _create(client, kind="competition", organizer="Teknofest", prize="50k TL", deadline="2026-12-01")

    result = client.post(f"/competitions/{created['id']}/to-map").json()

    node = queries.get_map_node(result["node_id"])
    assert node["kind"] == "competition"
    assert node["title"] == "NASA Space Apps"
    assert node["target_date"] == "2026-12-01"
    assert "Teknofest" in node["note"] and "50k TL" in node["note"]


def test_fellowships_land_on_the_map_as_targets(client: TestClient) -> None:
    created = _create(client, kind="fellowship")
    result = client.post(f"/competitions/{created['id']}/to-map").json()
    assert queries.get_map_node(result["node_id"])["kind"] == "target"


def test_page_renders_saved_entries_and_is_in_the_nav(client: TestClient) -> None:
    _create(client, title="Teknofest AI Challenge")

    response = client.get("/competitions")

    assert response.status_code == 200
    assert "Teknofest AI Challenge" in response.text
    assert "competitionsBoard(" in response.text
    assert ">Pursuits</span>" in client.get("/brief").text  # reached via the Pursuits entry…
    assert 'href="/competitions"' in response.text  # …and its tab row
