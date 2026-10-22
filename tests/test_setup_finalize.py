from pathlib import Path

import pytest

from command_center import config
from command_center.setup_wizard import finalize


@pytest.fixture()
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    profile_path = tmp_path / "profile.py"
    env_path = tmp_path / ".env"
    env_example_path = tmp_path / ".env.example"
    env_example_path.write_text(
        "# comment kept from the example\nTRIAGE_PROVIDER=ollama\n", encoding="utf-8"
    )
    monkeypatch.setattr(finalize, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(finalize, "ENV_PATH", env_path)
    monkeypatch.setattr(finalize, "ENV_EXAMPLE_PATH", env_example_path)
    return {"profile": profile_path, "env": env_path, "env_example": env_example_path}


def _sample_profile() -> dict:
    return {
        "name": "Ada Lovelace",
        "title": "Mathematician",
        "tagline": "I write the first algorithm.",
        "projects": [
            {"name": "Analytical Engine notes", "sentence": "Annotated translation.", "status_tag": "Shipping", "link": None},
        ],
    }


def test_render_profile_py_produces_valid_python() -> None:
    content = finalize._render_profile_py(_sample_profile())
    namespace: dict = {}
    exec(compile(content, "<test>", "exec"), namespace)
    assert namespace["PROFILE"]["name"] == "Ada Lovelace"
    assert namespace["PROFILE"]["initials"] == "AL"
    assert namespace["PROJECTS"][0]["name"] == "Analytical Engine notes"
    assert namespace["LANE_LABELS"]["urgent"] == "Urgent"


def test_render_profile_py_safely_escapes_quotes_and_special_characters() -> None:
    profile = _sample_profile()
    profile["name"] = 'Weird "Name" with \'quotes\' and a backslash \\'
    content = finalize._render_profile_py(profile)
    namespace: dict = {}
    exec(compile(content, "<test>", "exec"), namespace)  # must not raise SyntaxError
    assert namespace["PROFILE"]["name"] == profile["name"]


def test_write_profile_writes_file_and_hot_patches_config(
    isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PROFILE", {"name": "placeholder"}, raising=False)
    monkeypatch.setattr(config, "PROJECTS", [], raising=False)
    monkeypatch.setattr(config, "FOOTER_LINKS", {}, raising=False)
    monkeypatch.setattr(config, "BUILD_LOG", [], raising=False)
    monkeypatch.setattr(config, "LANE_LABELS", {}, raising=False)
    monkeypatch.setattr(config, "MEDIUM_RANKING_CONTEXT", "", raising=False)

    finalize._write_profile(_sample_profile())

    assert isolated_paths["profile"].exists()
    assert config.PROFILE["name"] == "Ada Lovelace"
    assert config.PROJECTS[0]["name"] == "Analytical Engine notes"


def test_upsert_env_seeds_from_example_when_env_missing(
    isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "_WIZARD_TEST_KEY", "", raising=False)
    assert not isolated_paths["env"].exists()

    finalize._upsert_env("_WIZARD_TEST_KEY", "value1")

    content = isolated_paths["env"].read_text(encoding="utf-8")
    assert "# comment kept from the example" in content  # seeded from .env.example
    assert "TRIAGE_PROVIDER=ollama" in content  # untouched
    assert "_WIZARD_TEST_KEY=value1" in content


def test_upsert_env_replaces_existing_key_in_place(
    isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "_WIZARD_TEST_KEY", "", raising=False)
    isolated_paths["env"].write_text("SOME_OTHER=1\n_WIZARD_TEST_KEY=old\nAFTER=2\n", encoding="utf-8")

    finalize._upsert_env("_WIZARD_TEST_KEY", "new")

    lines = isolated_paths["env"].read_text(encoding="utf-8").splitlines()
    assert lines == ["SOME_OTHER=1", "_WIZARD_TEST_KEY=new", "AFTER=2"]


def test_upsert_env_appends_when_key_not_present(
    isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "_WIZARD_TEST_KEY", "", raising=False)
    isolated_paths["env"].write_text("SOME_OTHER=1\n", encoding="utf-8")

    finalize._upsert_env("_WIZARD_TEST_KEY", "value")

    content = isolated_paths["env"].read_text(encoding="utf-8")
    assert "SOME_OTHER=1" in content
    assert "_WIZARD_TEST_KEY=value" in content


def test_finish_writes_profile_and_env_atomically(
    isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    for attr in ("PROFILE", "PROJECTS", "FOOTER_LINKS", "BUILD_LOG", "LANE_LABELS", "MEDIUM_RANKING_CONTEXT"):
        monkeypatch.setattr(config, attr, None, raising=False)
    for attr in ("GROQ_API_KEY", "TRIAGE_PROVIDER", "TRIAGE_MODEL", "MEDIUM_API_KEY", "MEDIUM_USERNAME"):
        monkeypatch.setattr(config, attr, None, raising=False)

    state = {
        "profile": _sample_profile(),
        "credentials": {
            "groq_api_key": "gsk_fake_key_value",
            "medium_api_key": "medium-key",
            "medium_username": "adalovelace",
        },
    }

    finalize.finish(state)

    assert isolated_paths["profile"].exists()
    assert config.PROFILE["name"] == "Ada Lovelace"
    assert config.GROQ_API_KEY == "gsk_fake_key_value"
    assert config.TRIAGE_PROVIDER == "groq"
    assert config.MEDIUM_API_KEY == "medium-key"
    assert config.MEDIUM_USERNAME == "adalovelace"

    env_content = isolated_paths["env"].read_text(encoding="utf-8")
    assert "GROQ_API_KEY=gsk_fake_key_value" in env_content
    assert "MEDIUM_USERNAME=adalovelace" in env_content


def test_finish_skips_medium_env_vars_when_not_provided(
    isolated_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    for attr in ("PROFILE", "PROJECTS", "FOOTER_LINKS", "BUILD_LOG", "LANE_LABELS", "MEDIUM_RANKING_CONTEXT"):
        monkeypatch.setattr(config, attr, None, raising=False)
    for attr in ("GROQ_API_KEY", "TRIAGE_PROVIDER", "TRIAGE_MODEL", "MEDIUM_API_KEY", "MEDIUM_USERNAME"):
        monkeypatch.setattr(config, attr, None, raising=False)

    state = {
        "profile": _sample_profile(),
        "credentials": {"groq_api_key": "gsk_fake_key_value", "medium_api_key": "", "medium_username": ""},
    }

    finalize.finish(state)

    env_content = isolated_paths["env"].read_text(encoding="utf-8")
    assert "MEDIUM_API_KEY" not in env_content
    assert "MEDIUM_USERNAME" not in env_content
