"""Google OAuth installed-app flow. Run once via `make auth` (or this module
directly); every subsequent run is headless, reading the saved token.
"""

import sys

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from command_center.config import GOOGLE_CREDENTIALS_PATH, GOOGLE_SCOPES, GOOGLE_TOKEN_PATH


class AuthNotConfigured(Exception):
    """No usable Google credentials on disk yet — caller should fall back."""


def _restrict_to_owner(path) -> None:
    """Token files carry live Gmail/Calendar/Tasks access — restrict to
    the owner on any OS where chmod means something (a no-op ACL-wise on
    Windows, but harmless there rather than erroring)."""
    try:
        path.chmod(0o600)
    except OSError:
        pass


def has_valid_credentials() -> bool:
    return GOOGLE_TOKEN_PATH.exists()


def run_installed_app_flow() -> None:
    """Interactive, browser-based consent. Only works on a machine with a
    real browser and your own Google login — not something that can be
    automated on your behalf.
    """
    if not GOOGLE_CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"{GOOGLE_CREDENTIALS_PATH} not found — download it from Google Cloud "
            "Console (OAuth client, Desktop app type) and place it at the repo root."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(GOOGLE_CREDENTIALS_PATH), list(GOOGLE_SCOPES)
    )
    creds = flow.run_local_server(port=0)
    GOOGLE_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    _restrict_to_owner(GOOGLE_TOKEN_PATH)
    print(f"Saved credentials to {GOOGLE_TOKEN_PATH}")


def get_google_credentials() -> Credentials:
    if not GOOGLE_TOKEN_PATH.exists():
        raise AuthNotConfigured("No saved Google token — run `make auth` first.")

    creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_PATH), list(GOOGLE_SCOPES))

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise AuthNotConfigured(
                "Google refresh token is no longer valid (invalid_grant) — likely "
                "the OAuth app is still in Testing status, where tokens expire "
                "after 7 days. Switch it to Production in Google Cloud Console, "
                "then run `make auth` again."
            ) from exc
        GOOGLE_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        _restrict_to_owner(GOOGLE_TOKEN_PATH)

    return creds


if __name__ == "__main__":
    try:
        run_installed_app_flow()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
