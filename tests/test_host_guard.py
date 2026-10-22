"""DNS-rebinding / cross-site protection (host_guard.py): a request with a
foreign Host is refused before it reaches any route, and a state-changing
request from a foreign Origin is refused too — while the app keeps
working normally from localhost."""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import attachments, auth, db, host_guard, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.setup_wizard import status as setup_status

EVIL = "http://evil.example.com"
LOCAL = "http://localhost:8000"


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", tmp_path / "attachments")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app as fastapi_app

    with TestClient(fastapi_app):  # runs startup once (DB init)
        pass
    return fastapi_app


@pytest.fixture()
def spawned(monkeypatch: pytest.MonkeyPatch) -> list:
    """Records any process the launch route tries to start, instead of
    starting it."""
    calls: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    return calls


def _project(tmp_path: Path) -> int:
    return queries.create_registered_project("Victim", str(tmp_path), "")


# The guard's own refusals — checked by body, not just status, so a test
# can't pass merely because the route itself happened to 400/404.
REFUSED_HOST = "Invalid host header"
REFUSED_ORIGIN = "Cross-site request refused"


# --- foreign Host: refused outright ---------------------------------------------


def test_foreign_host_cannot_read_the_brief(app) -> None:
    response = TestClient(app, base_url=EVIL).get("/brief")
    assert (response.status_code, response.text) == (400, REFUSED_HOST)


def test_foreign_host_cannot_register_a_project(app, tmp_path: Path) -> None:
    response = TestClient(app, base_url=EVIL).post(
        "/projects", json={"name": "x", "path": str(tmp_path), "run_command": "calc.exe"}
    )
    assert (response.status_code, response.text) == (400, REFUSED_HOST)
    assert queries.list_registered_projects() == []


def test_foreign_host_cannot_launch_a_project(app, tmp_path: Path, spawned: list) -> None:
    project_id = _project(tmp_path)
    response = TestClient(app, base_url=EVIL).post(f"/projects/{project_id}/launch")
    assert (response.status_code, response.text) == (400, REFUSED_HOST)
    assert spawned == []


def test_foreign_host_cannot_enable_and_pull_all_sources(app) -> None:
    queries.update_source_config("gmail", enabled=False)
    response = TestClient(app, base_url=EVIL).post("/settings/sources/pull-all")
    assert (response.status_code, response.text) == (400, REFUSED_HOST)
    gmail = next(c for c in queries.list_source_configs() if c["source_name"] == "gmail")
    assert not gmail["enabled"]


def test_localhost_still_works(app, tmp_path: Path) -> None:
    for base in (LOCAL, "http://127.0.0.1:8000"):
        client = TestClient(app, base_url=base)
        assert client.get("/brief").status_code == 200
    created = TestClient(app, base_url=LOCAL).post("/projects", json={"name": "Mine", "path": str(tmp_path)})
    assert created.status_code == 200


# --- foreign Origin on a state-changing request: refused ---------------------------


def test_cross_site_post_is_refused_even_with_a_local_host(app, tmp_path: Path, spawned: list) -> None:
    project_id = _project(tmp_path)
    client = TestClient(app, base_url=LOCAL)

    launch = client.post(f"/projects/{project_id}/launch", headers={"Origin": EVIL})
    delete = client.delete(f"/projects/{project_id}", headers={"Origin": EVIL})
    sandboxed = client.post("/projects", headers={"Origin": "null"}, json={"name": "x", "path": str(tmp_path)})

    for refused in (launch, delete, sandboxed):
        assert (refused.status_code, refused.json()["detail"]) == (403, REFUSED_ORIGIN)
    assert spawned == []
    assert queries.get_registered_project(project_id) is not None


def test_same_origin_and_originless_writes_are_allowed(app, tmp_path: Path) -> None:
    client = TestClient(app, base_url=LOCAL)
    with_origin = client.post(
        "/projects", headers={"Origin": "http://localhost:8000"}, json={"name": "A", "path": str(tmp_path)}
    )
    without_origin = client.post("/projects", json={"name": "B", "path": str(tmp_path)})
    assert (with_origin.status_code, without_origin.status_code) == (200, 200)


def test_reads_are_not_origin_checked(app) -> None:
    # GETs can't change anything; the Host check is what protects them.
    assert TestClient(app, base_url=LOCAL).get("/brief", headers={"Origin": EVIL}).status_code == 200


# --- configuration -------------------------------------------------------------------


def test_allowed_hosts_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    assert host_guard.allowed_hosts() == ["localhost", "127.0.0.1"]
    monkeypatch.setenv("ALLOWED_HOSTS", " localhost , my-box.lan ")
    assert host_guard.allowed_hosts() == ["localhost", "my-box.lan"]
