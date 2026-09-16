"""license_gate.verify()/require() always take the pytest bypass in every
OTHER test in this suite (see license_gate.py's own docstring for why).
These tests deliberately unset PYTEST_CURRENT_TEST to exercise the real,
non-bypassed check — otherwise this module would be the one thing in the
whole app with no real test coverage at all.
"""

import hashlib

import pytest

from command_center import license_gate


@pytest.fixture()
def real_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removes the pytest bypass for the duration of one test. Patches
    _running_under_pytest() itself rather than the PYTEST_CURRENT_TEST
    env var it reads — pytest resets that var during a test's own
    lifecycle, so deleting it from inside a test doesn't reliably stick.
    """
    monkeypatch.setattr(license_gate, "_running_under_pytest", lambda: False)


def test_verify_true_with_correct_key_via_env_var(monkeypatch: pytest.MonkeyPatch, real_check: None) -> None:
    key = "a-real-key-for-this-test"
    monkeypatch.setattr(license_gate, "_EXPECTED_KEY_HASH", hashlib.sha256(key.encode()).hexdigest())
    monkeypatch.setenv("COMMAND_CENTER_LICENSE_KEY", key)

    assert license_gate.verify() is True


def test_verify_false_with_wrong_key(monkeypatch: pytest.MonkeyPatch, real_check: None) -> None:
    monkeypatch.setattr(license_gate, "_EXPECTED_KEY_HASH", hashlib.sha256(b"the-real-key").hexdigest())
    monkeypatch.setenv("COMMAND_CENTER_LICENSE_KEY", "a-guessed-key")

    assert license_gate.verify() is False


def test_verify_false_with_no_key_at_all(monkeypatch: pytest.MonkeyPatch, tmp_path, real_check: None) -> None:
    monkeypatch.delenv("COMMAND_CENTER_LICENSE_KEY", raising=False)
    # Point the file check at an empty directory so a real license.key
    # sitting in this repo's own root (the developer's own key) can't
    # accidentally make this test pass.
    monkeypatch.setattr(license_gate, "_LICENSE_KEY_FILE", tmp_path / "license.key")

    assert license_gate.verify() is False


def test_verify_true_with_correct_key_via_file(monkeypatch: pytest.MonkeyPatch, tmp_path, real_check: None) -> None:
    key = "a-real-key-from-a-file"
    monkeypatch.setattr(license_gate, "_EXPECTED_KEY_HASH", hashlib.sha256(key.encode()).hexdigest())
    monkeypatch.delenv("COMMAND_CENTER_LICENSE_KEY", raising=False)
    key_file = tmp_path / "license.key"
    key_file.write_text(key + "\n")  # trailing newline, like a human-saved file
    monkeypatch.setattr(license_gate, "_LICENSE_KEY_FILE", key_file)

    assert license_gate.verify() is True


def test_env_var_takes_priority_over_file(monkeypatch: pytest.MonkeyPatch, tmp_path, real_check: None) -> None:
    correct_key = "correct-key"
    monkeypatch.setattr(license_gate, "_EXPECTED_KEY_HASH", hashlib.sha256(correct_key.encode()).hexdigest())
    monkeypatch.setenv("COMMAND_CENTER_LICENSE_KEY", correct_key)
    key_file = tmp_path / "license.key"
    key_file.write_text("a-stale-wrong-key")
    monkeypatch.setattr(license_gate, "_LICENSE_KEY_FILE", key_file)

    assert license_gate.verify() is True


def test_require_raises_when_unlicensed(monkeypatch: pytest.MonkeyPatch, real_check: None) -> None:
    monkeypatch.setattr(license_gate, "verify", lambda: False)

    with pytest.raises(RuntimeError, match="not licensed to run"):
        license_gate.require()


def test_require_is_silent_when_licensed(monkeypatch: pytest.MonkeyPatch, real_check: None) -> None:
    monkeypatch.setattr(license_gate, "verify", lambda: True)

    license_gate.require()  # must not raise


def test_pytest_bypass_short_circuits_even_a_bad_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # No real_check fixture here — this confirms the bypass every OTHER
    # test in the suite relies on actually works, with an obviously
    # wrong expected hash in place to prove it isn't coincidentally
    # passing because of a real key sitting in this repo's own root.
    monkeypatch.setattr(license_gate, "_EXPECTED_KEY_HASH", "not-a-real-hash")
    monkeypatch.delenv("COMMAND_CENTER_LICENSE_KEY", raising=False)

    assert license_gate.verify() is True
