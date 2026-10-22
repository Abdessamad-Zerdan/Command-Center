"""ntfy.sh push notifications. Every call degrades to a logged failure,
never raises — same "external call can't take down the brief"
philosophy as every source in pipeline.py, just for a push instead of
a pull.
"""

import logging

import httpx

from command_center.config import NTFY_SERVER, NTFY_TOPIC

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(NTFY_TOPIC)


def send(title: str, message: str, priority: str = "default", tags: list[str] | None = None) -> bool:
    """Returns True on a successful publish, False on any failure
    (network error, non-2xx, or NTFY_TOPIC unset) — callers that need to
    surface a failure to a user (the Settings test-send button) check
    the return value; callers that are just a background nudge don't."""
    if not is_configured():
        return False
    try:
        response = httpx.post(
            f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}",
            content=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                **({"Tags": ",".join(tags)} if tags else {}),
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.exception("ntfy publish failed (title=%r)", title)
        return False
