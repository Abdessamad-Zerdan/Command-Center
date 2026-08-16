from pathlib import Path

import pytest
from google.auth.exceptions import RefreshError

from command_center import auth


@pytest.fixture()
def token_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "google_token.json"
    monkeypatch.setattr(auth, "GOOGLE_TOKEN_PATH", path)
    return path


def test_has_valid_credentials_false_when_no_token_file(token_path: Path) -> None:
    assert auth.has_valid_credentials() is False


def test_has_valid_credentials_true_when_token_file_exists(token_path: Path) -> None:
    token_path.write_text("{}", encoding="utf-8")
    assert auth.has_valid_credentials() is True


def test_get_google_credentials_raises_auth_not_configured_without_a_token(
    token_path: Path,
) -> None:
    with pytest.raises(auth.AuthNotConfigured):
        auth.get_google_credentials()


def test_run_installed_app_flow_raises_file_not_found_without_credentials_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "GOOGLE_CREDENTIALS_PATH", tmp_path / "does-not-exist.json")
    with pytest.raises(FileNotFoundError, match="not found"):
        auth.run_installed_app_flow()


class _FakeCredentials:
    def __init__(self, expired: bool, refresh_token: str | None, raise_on_refresh: bool) -> None:
        self.expired = expired
        self.refresh_token = refresh_token
        self._raise_on_refresh = raise_on_refresh
        self.refreshed = False

    def refresh(self, request) -> None:
        if self._raise_on_refresh:
            raise RefreshError("invalid_grant: Token has been expired or revoked.")
        self.refreshed = True
        self.expired = False

    def to_json(self) -> str:
        return '{"refreshed": true}'


def test_get_google_credentials_wraps_invalid_grant_as_auth_not_configured(
    token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path.write_text("{}", encoding="utf-8")
    fake = _FakeCredentials(expired=True, refresh_token="rt", raise_on_refresh=True)
    monkeypatch.setattr(auth.Credentials, "from_authorized_user_file", lambda *a, **k: fake)

    with pytest.raises(auth.AuthNotConfigured, match="invalid_grant"):
        auth.get_google_credentials()


def test_get_google_credentials_refreshes_and_persists_expired_token(
    token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path.write_text("{}", encoding="utf-8")
    fake = _FakeCredentials(expired=True, refresh_token="rt", raise_on_refresh=False)
    monkeypatch.setattr(auth.Credentials, "from_authorized_user_file", lambda *a, **k: fake)

    result = auth.get_google_credentials()

    assert result is fake
    assert fake.refreshed is True
    assert token_path.read_text(encoding="utf-8") == '{"refreshed": true}'


def test_get_google_credentials_does_not_refresh_when_not_expired(
    token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path.write_text("original", encoding="utf-8")
    fake = _FakeCredentials(expired=False, refresh_token="rt", raise_on_refresh=True)
    monkeypatch.setattr(auth.Credentials, "from_authorized_user_file", lambda *a, **k: fake)

    result = auth.get_google_credentials()

    assert result is fake
    # untouched — refresh() would have raised if it had been called
    assert token_path.read_text(encoding="utf-8") == "original"
