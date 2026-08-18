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
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(config, "GROQ_API_KEY", None)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    # "groq" with no key, not "ollama"/"auto" — those need no key at all
    # (see status.py's triage_ready check), so leaving this unpatched
    # would make is_setup_complete() depend on whatever TRIAGE_PROVIDER
    # the real .env this machine happens to have actually set, instead
    # of the "nothing configured yet" state this whole file exercises.
    monkeypatch.setattr(config, "TRIAGE_PROVIDER", "groq")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", None)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", None)
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_PATH", tmp_path / "no_credentials.json")
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)

    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def _future(days: int = 7) -> str:
    return (datetime.now(TZ) + timedelta(days=days)).isoformat()


def _past(days: int = 1) -> str:
    return (datetime.now(TZ) - timedelta(days=days)).isoformat()


# --- queries CRUD ------------------------------------------------------------


def test_create_and_get_setup_invite(client: TestClient) -> None:
    token = invites.generate_token()
    queries.create_setup_invite("someone@example.com", token, _future())

    row = queries.get_setup_invite_by_token(token)

    assert row["email"] == "someone@example.com"
    assert row["used_at"] is None
    assert row["used_by_ip"] is None


def test_get_setup_invite_by_token_unknown_returns_none(client: TestClient) -> None:
    assert queries.get_setup_invite_by_token("does-not-exist") is None


def test_list_setup_invites_newest_first(client: TestClient) -> None:
    queries.create_setup_invite("first@example.com", invites.generate_token(), _future())
    queries.create_setup_invite("second@example.com", invites.generate_token(), _future())

    rows = queries.list_setup_invites()

    assert [r["email"] for r in rows] == ["second@example.com", "first@example.com"]


def test_mark_setup_invite_used_sets_timestamp_and_ip(client: TestClient) -> None:
    token = invites.generate_token()
    queries.create_setup_invite("someone@example.com", token, _future())

    queries.mark_setup_invite_used(token, "127.0.0.1")

    row = queries.get_setup_invite_by_token(token)
    assert row["used_at"] is not None
    assert row["used_by_ip"] == "127.0.0.1"


# --- invite_status -------------------------------------------------------------


def test_invite_status_pending(client: TestClient) -> None:
    token = invites.generate_token()
    queries.create_setup_invite("a@example.com", token, _future())
    row = queries.get_setup_invite_by_token(token)
    assert invites.invite_status(row) == "pending"


def test_invite_status_used(client: TestClient) -> None:
    token = invites.generate_token()
    queries.create_setup_invite("a@example.com", token, _future())
    queries.mark_setup_invite_used(token, None)
    row = queries.get_setup_invite_by_token(token)
    assert invites.invite_status(row) == "used"


def test_invite_status_expired(client: TestClient) -> None:
    token = invites.generate_token()
    queries.create_setup_invite("a@example.com", token, _past())
    row = queries.get_setup_invite_by_token(token)
    assert invites.invite_status(row) == "expired"


# --- wizard gating -------------------------------------------------------------


def test_setup_wizard_is_open_when_no_invite_has_ever_been_created(client: TestClient) -> None:
    # TEMPORARY first-run bypass (see invites.request_is_invited's own
    # docstring): a genuinely fresh instance that has never created a
    # single invite has no way to hand itself one, so the wizard is open
    # with no token needed. Closes the moment any real invite exists —
    # see the tests below.
    response = client.get("/setup/step/1")
    assert response.status_code == 200
    assert "Step 1 of 10" in response.text


def test_setup_with_unknown_invite_token_is_blocked_once_a_real_invite_exists(
    client: TestClient,
) -> None:
    # The first-run bypass only applies while zero invites exist at all —
    # once a real one has been created, an unrelated garbage token must
    # still be rejected, not silently let through.
    queries.create_setup_invite("a@example.com", invites.generate_token(), _future())

    response = client.get("/setup/step/1?invite=not-a-real-token")

    assert response.status_code == 403


def test_setup_with_expired_invite_is_blocked(client: TestClient) -> None:
    token = invites.generate_token()
    queries.create_setup_invite("a@example.com", token, _past())

    response = client.get(f"/setup/step/1?invite={token}")

    assert response.status_code == 403


def test_setup_with_already_used_invite_is_blocked(client: TestClient) -> None:
    token = invites.generate_token()
    queries.create_setup_invite("a@example.com", token, _future())
    queries.mark_setup_invite_used(token, None)

    response = client.get(f"/setup/step/1?invite={token}")

    assert response.status_code == 403


def test_setup_with_valid_invite_query_param_proceeds(client: TestClient) -> None:
    token = invites.generate_token()
    queries.create_setup_invite("a@example.com", token, _future())

    response = client.get(f"/setup/step/1?invite={token}")

    assert response.status_code == 200
    assert "Step 1 of 10" in response.text


def test_valid_invite_persists_in_state_so_later_steps_skip_the_query_param(
    client: TestClient,
) -> None:
    token = invites.generate_token()
    queries.create_setup_invite("a@example.com", token, _future())

    first = client.get(f"/setup/step/1?invite={token}")
    assert first.status_code == 200

    # No ?invite= this time — must still work because it's now stored in
    # setup_state.json from the first request.
    second = client.get("/setup/step/2")
    assert second.status_code == 200

    state = state_module.load()
    assert state["invite_token"] == token


def test_settings_invites_page_redirects_to_setup_while_incomplete(client: TestClient) -> None:
    response = client.get("/settings/invites", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


# --- finish marks the invite used, one invite one use -------------------------


def test_finish_marks_invite_used_and_it_cannot_be_reused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.setup_wizard import router

    monkeypatch.setattr(router.triage, "_run_groq", lambda items: [{"lane": "urgent"}])
    monkeypatch.setattr(
        router.auth,
        "get_google_credentials",
        lambda: (_ for _ in ()).throw(auth.AuthNotConfigured("no token yet")),
    )

    token = invites.generate_token()
    queries.create_setup_invite("a@example.com", token, _future())

    client.get(f"/setup/step/1?invite={token}")
    client.post(
        "/setup/step/2",
        json={"name": "Ada Lovelace", "title": "Mathematician", "tagline": "First algorithm."},
    )
    client.post(
        "/setup/step/7",
        json={"groq_api_key": "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"},
    )
    client.post("/setup/step/8", json={"skip": True})
    client.post("/setup/step/9")
    client.post("/setup/step/10")

    finish = client.post("/setup/finish")
    assert finish.status_code == 200

    row = queries.get_setup_invite_by_token(token)
    assert row["used_at"] is not None

    # A brand-new attempt (fresh state, since setup is now complete the
    # gate wouldn't even trigger — but the invite itself is provably
    # spent regardless) can't be validated again.
    assert invites.invite_status(row) == "used"


# --- admin page, once already set up -------------------------------------------


@pytest.fixture()
def complete_client(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: True)
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_fake")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "123-abc.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "GOCSPX-fake")
    monkeypatch.setattr(config, "PROFILE", {"name": "Someone Real"})
    return client


def test_settings_invites_page_reachable_once_setup_is_complete(
    complete_client: TestClient,
) -> None:
    response = complete_client.get("/settings/invites")
    assert response.status_code == 200
    assert "Generate invite" in response.text


def test_create_invite_returns_a_setup_link(complete_client: TestClient) -> None:
    response = complete_client.post(
        "/settings/invites", json={"email": "new-person@example.com", "expiry_days": 3}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["invite"]["email"] == "new-person@example.com"
    assert data["invite"]["link"].startswith("/setup?invite=")
    assert data["invite"]["status"] == "pending"


def test_create_invite_rejects_empty_email(complete_client: TestClient) -> None:
    response = complete_client.post("/settings/invites", json={"email": "   "})
    assert response.status_code == 400
    assert "email" in response.json()["errors"]


def test_create_invite_rejects_non_positive_expiry(complete_client: TestClient) -> None:
    response = complete_client.post(
        "/settings/invites", json={"email": "a@example.com", "expiry_days": 0}
    )
    assert response.status_code == 400
    assert "expiry_days" in response.json()["errors"]


def test_created_invite_appears_in_the_page_list(complete_client: TestClient) -> None:
    complete_client.post("/settings/invites", json={"email": "listed@example.com"})

    response = complete_client.get("/settings/invites")

    assert "listed@example.com" in response.text
