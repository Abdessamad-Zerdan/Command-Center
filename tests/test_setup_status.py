from pathlib import Path

import pytest

from command_center import auth, config, profile_example
from command_center.setup_wizard import status


@pytest.fixture()
def satisfied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every check passing — tests then break one thing at a time."""
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_fake")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(config, "TRIAGE_PROVIDER", "groq")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: True)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "123-abc.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "GOCSPX-fake")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_PATH", tmp_path / "no_credentials_here.json")
    monkeypatch.setattr(config, "PROFILE", {"name": "Real Name"})


def test_complete_when_everything_satisfied(satisfied: None) -> None:
    assert status.is_setup_complete() is True


def test_incomplete_without_groq_anthropic_or_ollama(
    satisfied: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "GROQ_API_KEY", None)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(config, "TRIAGE_PROVIDER", "something-unconfigured")
    assert status.is_setup_complete() is False


def test_ollama_provider_alone_satisfies_the_provider_check(
    satisfied: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lockout this guards against: TRIAGE_PROVIDER=ollama is the
    documented default (.env.example) and needs no key at all — treating
    only a Groq/Anthropic key as "ready" would permanently redirect a
    correctly-set-up Ollama instance to /setup, which then 403s it for
    lacking an invite."""
    monkeypatch.setattr(config, "GROQ_API_KEY", None)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(config, "TRIAGE_PROVIDER", "ollama")
    assert status.is_setup_complete() is True


def test_auto_provider_alone_satisfies_the_provider_check(
    satisfied: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same lockout, same reasoning, for TRIAGE_PROVIDER=auto — the new
    .env.example default. auto's whole point is working without a Groq
    key configured yet (it only needs one once Ollama is unavailable),
    so it must satisfy this check exactly like plain "ollama" does."""
    monkeypatch.setattr(config, "GROQ_API_KEY", None)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(config, "TRIAGE_PROVIDER", "auto")
    assert status.is_setup_complete() is True


def test_anthropic_alone_satisfies_the_provider_check(
    satisfied: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "GROQ_API_KEY", None)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-fake")
    assert status.is_setup_complete() is True


def test_complete_even_without_any_google_credentials(
    satisfied: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The lockout regression this guards against: the built wizard steps
    (STEP_ORDER in state.py) don't collect Google credentials at all —
    that's the still-unbuilt Part 2 — so a Groq-only finish must still
    count as complete, or invite-gating + this check would permanently
    bounce a freshly-finished user back to an already-used, now-invalid
    /setup link with no recovery path."""
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", None)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", None)
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_PATH", tmp_path / "no_credentials_here.json")
    assert status.is_setup_complete() is True


def test_incomplete_when_profile_is_still_placeholder(
    satisfied: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PROFILE", profile_example.PROFILE)
    assert status.is_setup_complete() is False
