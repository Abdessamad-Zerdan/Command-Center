"""Placeholder personal content — copy this file to profile.py and edit it
with your own details. profile.py is gitignored (it's your data, not the
repo's); this file is the committed template config.py falls back to when
profile.py doesn't exist yet, so the app still boots out of the box.
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


PROFILE = {
    "name": "Your Name",
    "title": "Your Title",
    "tagline": "A one-line description of what you build or work on.",
    # Placeholder until the real photo is dropped in — see static/img/README.
    "photo_url": "/static/img/profile.jpg",
    "initials": "YN",
}

PROJECTS: list[Project] = [
    {
        "name": "Example Project",
        "sentence": "One-line description of what it does.",
        "status_tag": "Shipping",
        "link": None,
    },
]

# None until a real URL exists — the template hides the slot entirely rather
# than showing a placeholder.
FOOTER_LINKS = {
    "github": None,
}

# Most recent first — edit directly, no sorting/parsing applied.
BUILD_LOG: list[BuildLogEntry] = [
    {"date": "JAN 01", "text": "Example build log entry."},
]

# Display text for each brief lane. Keys must match config.LANES exactly —
# the dashboard/settings templates do a plain dict lookup per lane key.
LANE_LABELS = {
    "urgent": "Urgent",
    "action_items": "Action Items",
    "meeting_prep": "Meeting Prep",
    "tasks_due": "Tasks Due",
    "reading": "Reading",
}

# Fed into the Groq ranking prompt in sources/medium.py — free-text
# description of what the Reading lane should be ranked against.
MEDIUM_RANKING_CONTEXT = "software engineering, AI, and web development"
