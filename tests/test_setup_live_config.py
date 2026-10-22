import pytest

from command_center import config, triage
from command_center.setup_wizard import live_config


def test_patch_updates_config_and_every_dependent_module(monkeypatch: pytest.MonkeyPatch) -> None:
    # raising=False lets monkeypatch create+track a throwaway attribute
    # (and clean it up at teardown) so this test can't leak state into
    # later tests regardless of what patch() itself does under the hood.
    monkeypatch.setattr(config, "_WIZARD_TEST_ATTR", "before", raising=False)
    monkeypatch.setattr(triage, "_WIZARD_TEST_ATTR", "before", raising=False)

    live_config.patch("_WIZARD_TEST_ATTR", "after")

    assert config._WIZARD_TEST_ATTR == "after"
    assert triage._WIZARD_TEST_ATTR == "after"


def test_patch_skips_modules_that_never_had_the_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_WIZARD_TEST_ATTR_2", "before", raising=False)
    assert not hasattr(triage, "_WIZARD_TEST_ATTR_2")

    live_config.patch("_WIZARD_TEST_ATTR_2", "after")  # must not raise

    assert config._WIZARD_TEST_ATTR_2 == "after"
    assert not hasattr(triage, "_WIZARD_TEST_ATTR_2")


def test_apply_sets_process_env_var_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_WIZARD_TEST_ENV", "before")
    monkeypatch.setattr(config, "_WIZARD_TEST_ENV", "before", raising=False)

    live_config.apply("_WIZARD_TEST_ENV", "after")

    import os

    assert os.environ["_WIZARD_TEST_ENV"] == "after"
    assert config._WIZARD_TEST_ENV == "after"


def test_temporary_patch_reverts_after_the_with_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "GROQ_API_KEY", "original-key")
    monkeypatch.setattr(triage, "GROQ_API_KEY", "original-key")

    with live_config.temporary_patch("GROQ_API_KEY", "draft-key"):
        assert config.GROQ_API_KEY == "draft-key"
        assert triage.GROQ_API_KEY == "draft-key"

    assert config.GROQ_API_KEY == "original-key"
    assert triage.GROQ_API_KEY == "original-key"


def test_temporary_patch_reverts_even_if_the_block_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "GROQ_API_KEY", "original-key")

    with pytest.raises(RuntimeError):
        with live_config.temporary_patch("GROQ_API_KEY", "draft-key"):
            raise RuntimeError("simulated failure during the test call")

    assert config.GROQ_API_KEY == "original-key"
