"""The one place that writes real config. Everything the wizard collects
lives in secrets/setup_state.json (a draft) until finish() runs — that's
what "atomic" means here: either nothing real changed, or the user saw
step 10's test results and both profile.py and .env are written together.
"""

from command_center.config import REPO_ROOT
from command_center.setup_wizard import live_config

ENV_PATH = REPO_ROOT / ".env"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
PROFILE_PATH = REPO_ROOT / "src" / "command_center" / "profile.py"

# A real Groq-hosted model — the fresh-install default in config.py
# (TRIAGE_MODEL="llama3.2:3b") is an Ollama tag, not a Groq one, so
# step 10's live test needs this explicitly rather than whatever the
# unconfigured default happens to be.
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"

_PROFILE_SHAPED_KEYS = (
    "PROFILE",
    "PROJECTS",
    "FOOTER_LINKS",
    "BUILD_LOG",
    "LANE_LABELS",
    "MEDIUM_RANKING_CONTEXT",
)


def _derive_initials(name: str) -> str:
    words = name.split()
    initials = "".join(word[0].upper() for word in words[:2] if word)
    return initials or "??"


def _render_profile_py(profile_data: dict) -> str:
    name = profile_data["name"].strip()
    title = profile_data["title"].strip()
    tagline = profile_data["tagline"].strip()
    initials = _derive_initials(name)

    project_entries = []
    for project in profile_data.get("projects", []):
        project_entries.append(
            "    {\n"
            f"        \"name\": {project['name'].strip()!r},\n"
            f"        \"sentence\": {project['sentence'].strip()!r},\n"
            f"        \"status_tag\": {project.get('status_tag', 'Shipping')!r},\n"
            f"        \"link\": {(project.get('link') or None)!r},\n"
            "    },"
        )
    projects_block = "\n".join(project_entries)

    return f'''"""Personal/instance-specific content — written by the setup wizard.
Gitignored — this is your data, not the repo's. Safe to edit by hand.
"""

from typing import TypedDict


class Project(TypedDict):
    name: str
    sentence: str
    status_tag: str  # Shipping | Testing | Competing
    link: str | None


class BuildLogEntry(TypedDict):
    date: str
    text: str


PROFILE = {{
    "name": {name!r},
    "title": {title!r},
    "tagline": {tagline!r},
    "photo_url": "/static/img/profile.jpg",
    "initials": {initials!r},
}}

PROJECTS: list[Project] = [
{projects_block}
]

FOOTER_LINKS = {{
    "github": None,
}}

BUILD_LOG: list[BuildLogEntry] = []

# Display text for each brief lane. Keys must match config.LANES exactly.
LANE_LABELS = {{
    "urgent": "Urgent",
    "action_items": "Action Items",
    "meeting_prep": "Meeting Prep",
    "tasks_due": "Tasks Due",
    "reading": "Reading",
}}

# Fed into the Groq ranking prompt in sources/medium.py.
MEDIUM_RANKING_CONTEXT = "software engineering, AI, and web development"
'''


def _write_profile(profile_data: dict) -> None:
    content = _render_profile_py(profile_data)
    PROFILE_PATH.write_text(content, encoding="utf-8")

    # Exec the freshly-rendered source directly instead of going through
    # importlib — profile.py may not have been importable before this
    # write (fresh install, no file yet), so there's nothing to reload().
    # We already have the content in memory; no need to round-trip
    # through the import system to get the values back out of it.
    namespace: dict = {}
    exec(compile(content, str(PROFILE_PATH), "exec"), namespace)
    for key in _PROFILE_SHAPED_KEYS:
        live_config.patch(key, namespace[key])


def _upsert_env(key: str, value: str) -> None:
    if not ENV_PATH.exists():
        seed = ENV_EXAMPLE_PATH.read_text(encoding="utf-8") if ENV_EXAMPLE_PATH.exists() else ""
        ENV_PATH.write_text(seed, encoding="utf-8")

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    new_line = f"{key}={value}"
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    live_config.apply(key, value)


def finish(state: dict) -> None:
    _write_profile(state["profile"])

    creds = state["credentials"]
    _upsert_env("GROQ_API_KEY", creds["groq_api_key"])
    _upsert_env("TRIAGE_PROVIDER", "groq")
    _upsert_env("TRIAGE_MODEL", DEFAULT_GROQ_MODEL)
    if creds.get("medium_api_key") and creds.get("medium_username"):
        _upsert_env("MEDIUM_API_KEY", creds["medium_api_key"])
        _upsert_env("MEDIUM_USERNAME", creds["medium_username"])
