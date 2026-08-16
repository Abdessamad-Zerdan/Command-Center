"""Plain-language format checks — no network calls. The wizard's inline
errors are these messages verbatim, not raw exceptions.
"""

import re

_GROQ_KEY_RE = re.compile(r"^gsk_[A-Za-z0-9]{20,}$")


def validate_groq_key(value: str) -> str | None:
    """Returns an error message, or None if the key looks valid."""
    value = value.strip()
    if not value:
        return "Enter your Groq API key."
    if not _GROQ_KEY_RE.match(value):
        return "That doesn't look like a Groq key — it should start with gsk_ and be at least 24 characters."
    return None
