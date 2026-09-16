"""Enforces LICENSE's terms in code, not just on paper: this instance
won't actually run without a key that only the copyright holder hands
out. Checked at several independent, load-bearing points — app
startup, triage, and the assistant — rather than one single call a
clone could just delete and move on from; removing the app-startup
check alone still leaves triage and the assistant refusing to run.

Only a SHA-256 hash of the real key is ever committed (`_EXPECTED_KEY_HASH`
below) — the key itself lives in a gitignored `license.key` file at the
repo root, or the COMMAND_CENTER_LICENSE_KEY environment variable
(checked first, so it can be set without a file — useful for a
deployment where dropping a file in isn't convenient). Neither is ever
in git history.

No effort is made to obscure this file or how it's called — it's
meant to read exactly like what it is (a license check), the same as
any other licensed software's activation gate, not a hidden trap
someone stumbles into. Skipped automatically under pytest so the test
suite (and CI) never needs a key.
"""

import hashlib
import os
from pathlib import Path

from command_center.config import REPO_ROOT

_EXPECTED_KEY_HASH = "bc41c534dc2e7d58a4b4363d9c346a1e3b9a8e912817a4574b9a97647c845814"

_LICENSE_KEY_FILE = REPO_ROOT / "license.key"

_MESSAGE = (
    "This instance is not licensed to run. Command Center's source is "
    "public for portfolio purposes only — see LICENSE. If you've been "
    "given permission to run this, set the COMMAND_CENTER_LICENSE_KEY "
    "environment variable or create a license.key file at the repo "
    "root with the key you were given."
)


def _read_key() -> str | None:
    env_key = os.environ.get("COMMAND_CENTER_LICENSE_KEY")
    if env_key:
        return env_key.strip()
    if _LICENSE_KEY_FILE.exists():
        return _LICENSE_KEY_FILE.read_text(encoding="utf-8").strip()
    return None


def _running_under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def verify() -> bool:
    # The whole suite runs under pytest, including tests that spin up
    # the real FastAPI app (and therefore this gate) via TestClient —
    # a key requirement has no business blocking development/CI. A
    # separate function (rather than checking the env var inline) so
    # license_gate's OWN tests can monkeypatch just this bypass off
    # without fighting pytest's own env var management — pytest resets
    # PYTEST_CURRENT_TEST itself during a test's lifecycle, so deleting
    # it from inside a test doesn't reliably stick.
    if _running_under_pytest():
        return True
    key = _read_key()
    if not key:
        return False
    return hashlib.sha256(key.encode("utf-8")).hexdigest() == _EXPECTED_KEY_HASH


def require() -> None:
    if not verify():
        raise RuntimeError(_MESSAGE)
