from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, config, db, queries
from command_center.assistant import ingest as assistant_ingest
from command_center.config import TZ
from command_center.setup_wizard import finalize, invites
from command_center.setup_wizard import state as state_module


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(state_module, "STATE_PATH", tmp_path / "setup_state.json")
    monkeypatch.setattr(finalize, "PROFILE_PATH", tmp_path / "profile.py")
    monkeypatch.setattr(finalize, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(finalize, "ENV_EXAMPLE_PATH", tmp_path / ".env.example.does-not-exist")
    # Nothing configured yet — is_setup_complete() should be False, which
    # is exactly the case these tests exercise.
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(config, "GROQ_API_KEY", None)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    # "groq" with no key, not "ollama"/"auto" — those need no key at all
    # (see status.py), so leaving this unpatched would make
    # is_setup_complete() depend on whatever TRIAGE_PROVIDER the real
    # .env on this machine happens to have set, instead of the "nothing
    # configured yet" state this fixture is meant to represent.
    monkeypatch.setattr(config, "TRIAGE_PROVIDER", "groq")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", None)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", None)
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_PATH", tmp_path / "no_credentials.json")
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)

    from command_center.app import app

    with TestClient(app) as test_client:
        # This whole file predates invite-gating and exercises step-flow
        # behavior, not the gate itself (that's test_setup_invites.py) —
        # pre-seed a valid, already-stored invite so every test here
        # keeps reaching the wizard exactly as before.
        token = invites.generate_token()
        expires_at = (datetime.now(TZ) + timedelta(days=7)).isoformat()
        queries.create_setup_invite("invitee@example.com", token, expires_at)
        state = state_module.load()
        state["invite_token"] = token
        state_module.save(state)
        yield test_client


def test_incomplete_setup_redirects_every_route_to_setup(client: TestClient) -> None:
    for path in ("/", "/brief", "/settings"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"


def test_setup_root_redirects_to_current_step(client: TestClient) -> None:
    response = client.get("/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/step/1"


def test_static_assets_are_never_gated(client: TestClient) -> None:
    # Doesn't need to exist on disk — just must not be redirected to /setup.
    response = client.get("/static/does-not-exist.png", follow_redirects=False)
    assert response.status_code != 303


@pytest.mark.parametrize("step", [1, 2, 7, 8, 9, 10])
def test_each_built_step_renders(client: TestClient, step: int) -> None:
    response = client.get(f"/setup/step/{step}")
    assert response.status_code == 200
    assert f"Step {step} of 10" in response.text


def test_welcome_advances_to_profile(client: TestClient) -> None:
    response = client.post("/setup/step/1")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "next_url": "/setup/step/2"}


def test_profile_rejects_empty_name(client: TestClient) -> None:
    response = client.post(
        "/setup/step/2", json={"name": "", "title": "Engineer", "tagline": "I build things."}
    )
    assert response.status_code == 400
    assert "name" in response.json()["errors"]


def test_profile_accepts_valid_data_and_skips_to_step_7(client: TestClient) -> None:
    response = client.post(
        "/setup/step/2",
        json={
            "name": "Grace Hopper",
            "title": "Rear Admiral",
            "tagline": "I debug compilers.",
            "projects": [{"name": "COBOL", "sentence": "A business language.", "status_tag": "Shipping"}],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["next_url"] == "/setup/step/7"  # skips reserved steps 3-6

    state = state_module.load()
    assert state["profile"]["name"] == "Grace Hopper"
    assert state["current_step"] == 7


def test_groq_step_rejects_bad_format(client: TestClient) -> None:
    response = client.post("/setup/step/7", json={"groq_api_key": "not-a-real-key"})
    assert response.status_code == 400
    assert "groq_api_key" in response.json()["errors"]


def test_groq_step_accepts_valid_format_and_advances(client: TestClient) -> None:
    response = client.post(
        "/setup/step/7",
        json={"groq_api_key": "gsk_FAKEKEYFAKEKEYFAKEKEYFAKEKEYFAKEKEY00"},
    )
    assert response.status_code == 200
    assert response.json()["next_url"] == "/setup/step/8"


def test_medium_step_skip_leaves_fields_blank(client: TestClient) -> None:
    response = client.post("/setup/step/8", json={"skip": True})
    assert response.status_code == 200
    state = state_module.load()
    assert state["confirmations"]["medium_skipped"] is True
    assert state["credentials"]["medium_api_key"] == ""


def test_medium_step_requires_both_fields_together(client: TestClient) -> None:
    response = client.post(
        "/setup/step/8", json={"medium_api_key": "key-only", "medium_username": ""}
    )
    assert response.status_code == 400


def test_back_navigates_to_previous_built_step_and_keeps_data(client: TestClient) -> None:
    client.post(
        "/setup/step/2",
        json={"name": "Ada Lovelace", "title": "Mathematician", "tagline": "First algorithm."},
    )
    response = client.post("/setup/back", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/step/2"

    state = state_module.load()
    assert state["current_step"] == 2
    assert state["profile"]["name"] == "Ada Lovelace"  # not discarded


def test_run_tests_reports_groq_pass_and_google_fail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.setup_wizard import router

    monkeypatch.setattr(router.triage, "_run_groq", lambda items: [{"lane": "urgent"}])
    monkeypatch.setattr(
        router.auth,
        "get_google_credentials",
        lambda: (_ for _ in ()).throw(auth.AuthNotConfigured("no token yet")),
    )

    client.post(
        "/setup/step/7",
        json={"groq_api_key": "gsk_FAKEKEYFAKEKEYFAKEKEYFAKEKEYFAKEKEY00"},
    )
    client.post("/setup/step/8", json={"skip": True})

    response = client.post("/setup/step/10")
    assert response.status_code == 200
    data = response.json()
    assert data["results"]["groq"]["pass"] is True
    assert data["results"]["gmail"]["pass"] is False
    assert "no token yet" in data["results"]["gmail"]["message"]
    assert data["results"]["medium"]["pass"] is None  # skipped
    assert data["ready_to_finish"] is True


def test_finish_blocked_until_required_services_pass(client: TestClient) -> None:
    response = client.post("/setup/finish")
    assert response.status_code == 400
    assert "finish" in response.json()["errors"]


def test_finish_succeeds_after_groq_passes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from command_center.setup_wizard import router

    monkeypatch.setattr(router.triage, "_run_groq", lambda items: [{"lane": "urgent"}])
    monkeypatch.setattr(
        router.auth,
        "get_google_credentials",
        lambda: (_ for _ in ()).throw(auth.AuthNotConfigured("no token yet")),
    )

    client.post(
        "/setup/step/2",
        json={"name": "Ada Lovelace", "title": "Mathematician", "tagline": "First algorithm."},
    )
    client.post(
        "/setup/step/7",
        json={"groq_api_key": "gsk_FAKEKEYFAKEKEYFAKEKEYFAKEKEYFAKEKEY00"},
    )
    client.post("/setup/step/8", json={"skip": True})
    client.post("/setup/step/9")
    client.post("/setup/step/10")

    response = client.post("/setup/finish")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "next_url": "/"}
    assert (tmp_path / "profile.py").exists()
    assert config.PROFILE["name"] == "Ada Lovelace"
    assert config.GROQ_API_KEY == "gsk_FAKEKEYFAKEKEYFAKEKEYFAKEKEYFAKEKEY00"


def test_a_groq_only_finish_does_not_lock_the_wizard_back_out(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The end-to-end regression this whole session's task 1 fix guards
    against: walk the wizard exactly the way a real Groq-only, no-Google
    user would (skip Medium, no Google credentials at all), finish it,
    then confirm the gate actually lets a freshly-finished instance
    through to ordinary pages instead of bouncing it straight back to an
    already-used, now-invalid /setup link with no recovery path."""
    from command_center.setup_wizard import router

    monkeypatch.setattr(router.triage, "_run_groq", lambda items: [{"lane": "urgent"}])
    monkeypatch.setattr(
        router.auth,
        "get_google_credentials",
        lambda: (_ for _ in ()).throw(auth.AuthNotConfigured("no token yet")),
    )

    client.post(
        "/setup/step/2",
        json={"name": "Ada Lovelace", "title": "Mathematician", "tagline": "First algorithm."},
    )
    client.post(
        "/setup/step/7",
        json={"groq_api_key": "gsk_FAKEKEYFAKEKEYFAKEKEYFAKEKEYFAKEKEY00"},
    )
    client.post("/setup/step/8", json={"skip": True})
    client.post("/setup/step/9")
    client.post("/setup/step/10")

    finish = client.post("/setup/finish")
    assert finish.status_code == 200

    for path in ("/", "/brief", "/settings"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code != 303 or response.headers.get("location") != "/setup", (
            f"{path} redirected back to /setup right after a completed, Groq-only finish"
        )


def test_finish_degrades_instead_of_500ing_on_a_disk_write_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.setup_wizard import router

    monkeypatch.setattr(router.triage, "_run_groq", lambda items: [{"lane": "urgent"}])
    monkeypatch.setattr(
        router.auth,
        "get_google_credentials",
        lambda: (_ for _ in ()).throw(auth.AuthNotConfigured("no token yet")),
    )

    client.post(
        "/setup/step/2",
        json={"name": "Ada Lovelace", "title": "Mathematician", "tagline": "First algorithm."},
    )
    client.post(
        "/setup/step/7",
        json={"groq_api_key": "gsk_FAKEKEYFAKEKEYFAKEKEYFAKEKEYFAKEKEY00"},
    )
    client.post("/setup/step/8", json={"skip": True})
    client.post("/setup/step/9")
    client.post("/setup/step/10")

    def _raise(state):
        raise OSError("disk full")

    monkeypatch.setattr(router.finalize, "finish", _raise)

    response = client.post("/setup/finish")

    assert response.status_code == 500
    assert "finish" in response.json()["errors"]


def test_complete_setup_can_still_reopen_the_wizard(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Once set up, /setup must stay reachable — you can go back in and
    change a value (profile, Groq key, etc) instead of being permanently
    locked out just because is_setup_complete() is now true."""
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: True)
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_fake")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "123-abc.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "GOCSPX-fake")
    monkeypatch.setattr(config, "PROFILE", {"name": "Someone Real"})

    response = client.get("/setup/step/2")
    assert response.status_code == 200
    assert "Step 2 of 10" in response.text

    # Other app routes are still reachable too — the gate only ever
    # forces incomplete instances toward /setup, it doesn't do anything
    # special for complete ones.
    assert client.get("/brief").status_code == 200


def test_a_pre_wizard_credentials_json_instance_is_not_gated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact regression the OR-fallback in status.py exists to prevent:
    an instance that used the manual SETUP.md flow (credentials.json, no
    GOOGLE_CLIENT_ID/SECRET) must work immediately, wizard untouched."""
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: True)
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_fake")
    monkeypatch.setattr(config, "PROFILE", {"name": "Someone Real"})
    real_credentials_file = tmp_path / "real_credentials.json"
    real_credentials_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_PATH", real_credentials_file)

    response = client.get("/brief", follow_redirects=False)
    assert response.status_code != 303 or response.headers.get("location") != "/setup"
