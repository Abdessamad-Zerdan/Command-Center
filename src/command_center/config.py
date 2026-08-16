"""Shared config constants and minimal .env loading (no python-dotenv dependency)."""

import os
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = REPO_ROOT / "command_center.db"

TRIAGE_LANES = ("urgent", "action_items", "meeting_prep", "tasks_due")  # what triage.run() can choose
LANES = TRIAGE_LANES + ("reading",)  # everything the UI/DB iterate over

# Fixed for this pass — editable via Settings is a future consideration,
# not this round (same "flag, don't build" scope note as elsewhere).
FINANCE_CATEGORIES = ("Rent", "Food", "Transport", "Subscriptions", "Savings", "Other")
FINANCE_TYPES = ("spend", "income", "saving")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(REPO_ROOT / ".env")

# Environment setting, not personal bio content, so it lives here rather
# than in profile.py — neutral default so a fresh clone doesn't inherit
# anyone's specific timezone.
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "UTC")
TZ = ZoneInfo(APP_TIMEZONE)

# profile.py is gitignored (it's your data, not the repo's) — copy
# profile_example.py to profile.py and edit it. Falling back to the
# example module here means the app still boots with placeholder content
# instead of crashing if that step hasn't happened yet.
try:
    from command_center import profile as _profile
except ImportError:
    from command_center import profile_example as _profile

    warnings.warn(
        "src/command_center/profile.py not found — using placeholder "
        "values from profile_example.py. Copy it to profile.py and edit "
        "it with your own details.",
        stacklevel=1,
    )

PROFILE = _profile.PROFILE
PROJECTS = _profile.PROJECTS
FOOTER_LINKS = _profile.FOOTER_LINKS
BUILD_LOG = _profile.BUILD_LOG
LANE_LABELS = _profile.LANE_LABELS
MEDIUM_RANKING_CONTEXT = _profile.MEDIUM_RANKING_CONTEXT

SECRETS_DIR = REPO_ROOT / "secrets"
SECRETS_DIR.mkdir(exist_ok=True)

GOOGLE_CREDENTIALS_PATH = REPO_ROOT / "credentials.json"
GOOGLE_TOKEN_PATH = SECRETS_DIR / "google_token.json"

# Alternate to GOOGLE_CREDENTIALS_PATH — the setup wizard (Part 2) writes
# these instead of a credentials.json file. Unset until then; auth.py
# still reads the file today, nothing here changes that yet.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks",
)

TRIAGE_PROVIDER = os.environ.get("TRIAGE_PROVIDER", "ollama")
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "llama3.2:3b")

# Only used by TRIAGE_PROVIDER=auto's local leg — kept separate from
# TRIAGE_MODEL since that one's meant for whatever cloud provider auto
# falls back to (Groq), and the two use unrelated model-name formats.
# Deliberately no hardcoded default: which models are actually pulled
# varies per machine, so a guessed tag would just waste a failed call
# before falling back to Groq anyway. Leave unset and triage.py asks
# Ollama's own API what's installed instead; set this only to pin one
# specific model when more than one is pulled locally.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
# Optional additional Groq accounts' keys — tried in order whenever the
# one before it is rate-limited, invalid, or otherwise failing.
GROQ_API_KEY_BACKUP = os.environ.get("GROQ_API_KEY_BACKUP")
GROQ_API_KEY_BACKUP_2 = os.environ.get("GROQ_API_KEY_BACKUP_2")

MEDIUM_API_KEY = os.environ.get("MEDIUM_API_KEY")
MEDIUM_USERNAME = os.environ.get("MEDIUM_USERNAME")
MEDIUM_USER_ID_CACHE = SECRETS_DIR / "medium_user_id.json"
