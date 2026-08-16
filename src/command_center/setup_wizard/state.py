"""secrets/setup_state.json — draft wizard answers. Nothing here is real
config until finalize.finish() writes it into .env/profile.py; abandoning
the wizard mid-way leaves the real config files untouched.
"""

import json
from typing import Any

from command_center.config import SECRETS_DIR

STATE_PATH = SECRETS_DIR / "setup_state.json"

# Part 1 builds steps 1, 2, 7, 8, 9, 10 — 3-6 are reserved for Part 2's
# Google Cloud deep-link steps, not built yet. This is the one place that
# changes when Part 2 lands (steps 3-6 get inserted here).
STEP_ORDER = [1, 2, 7, 8, 9, 10]

TOTAL_STEPS = 10  # shown as "Step N of 10" regardless of how many are built


def _default_state() -> dict[str, Any]:
    return {
        "current_step": STEP_ORDER[0],
        "invite_token": None,
        "profile": {
            "name": "",
            "title": "",
            "tagline": "",
            "projects": [],
        },
        "credentials": {
            "groq_api_key": "",
            "medium_api_key": "",
            "medium_username": "",
            "google_client_id": "",
            "google_client_secret": "",
            "google_project_id": "",
        },
        "confirmations": {
            "medium_skipped": False,
        },
        "test_results": {
            "groq": None,
            "gmail": None,
            "calendar": None,
            "tasks": None,
            "medium": None,
        },
    }


def load() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return _default_state()
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def next_step(current: int) -> int | None:
    """None means current is the last built step — the caller finishes
    instead of navigating forward."""
    idx = STEP_ORDER.index(current)
    if idx + 1 >= len(STEP_ORDER):
        return None
    return STEP_ORDER[idx + 1]


def prev_step(current: int) -> int | None:
    """None means current is the first step — no Back button there."""
    idx = STEP_ORDER.index(current)
    if idx == 0:
        return None
    return STEP_ORDER[idx - 1]
