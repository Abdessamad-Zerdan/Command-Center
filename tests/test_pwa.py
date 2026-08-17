import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth
from command_center import db as db_module
from command_center.assistant import ingest as assistant_ingest
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def test_manifest_json_is_served_and_well_formed(client: TestClient) -> None:
    response = client.get("/static/manifest.json")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Daily Command Center"
    assert data["start_url"] == "/brief"
    assert data["display"] == "standalone"
    assert len(data["icons"]) == 2
    for icon in data["icons"]:
        assert icon["src"].startswith("/static/img/")


def test_manifest_icon_files_actually_exist_on_disk() -> None:
    manifest_path = Path(__file__).resolve().parent.parent / "static" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    static_root = manifest_path.parent
    for icon in data["icons"]:
        icon_path = static_root / icon["src"].removeprefix("/static/")
        assert icon_path.is_file(), f"{icon_path} referenced in manifest.json but missing on disk"


def test_base_html_links_the_manifest_and_icons(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert '<link rel="manifest" href="/static/manifest.json">' in response.text
    assert '<link rel="apple-touch-icon" href="/static/img/icon-192.png">' in response.text
    assert '<meta name="theme-color" content="#b5563a">' in response.text
