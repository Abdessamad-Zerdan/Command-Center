"""Invite-gating for /setup, backed by the setup_invites table (db.py).
Admin-generated only: a link is created from /settings/invites (reachable
only once an instance is already set up), sent to the intended person
manually, and consumed once by whoever visits /setup with it. No
outbound email, no self-service request flow.

Pure token/validity helpers here; DB access goes through queries.py,
matching every other feature's split (e.g. fsutils.py vs queries.py for
projects). request_is_invited() is the single function register.py's
gating middleware calls.
"""

import secrets
from datetime import datetime
from typing import Any

from command_center import queries
from command_center.config import TZ
from command_center.setup_wizard import state as state_module

DEFAULT_EXPIRY_DAYS = 7


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def _is_row_valid(row: dict[str, Any]) -> bool:
    if row["used_at"] is not None:
        return False
    return row["expires_at"] > datetime.now(TZ).isoformat()


def request_is_invited(state: dict[str, Any], query_token: str | None) -> bool:
    """Checks state["invite_token"] first (persisted from an earlier
    request in this same flow); falls back to a fresh ?invite= query
    param on first entry, storing it into state once validated so later
    steps don't need it in the URL. Re-validates against the DB on every
    call, not just the first — an invite that gets used or expires
    mid-flow blocks the rest of the wizard too.

    Note: setup_state.json is a single file for the whole app instance,
    not per-browser/session (this app has no session concept at all).
    Once a token is stored here it's valid for any visitor until it's
    used or expires — acceptable for a "temporary, few people, one
    setup at a time" instance, not a guarantee against concurrent
    visitors.
    """
    token = state.get("invite_token") or query_token
    if not token:
        return False
    row = queries.get_setup_invite_by_token(token)
    if row is None or not _is_row_valid(row):
        return False
    if state.get("invite_token") != token:
        state["invite_token"] = token
        state_module.save(state)
    return True


def invite_status(row: dict[str, Any]) -> str:
    """"used" | "expired" | "pending" — for the admin list display."""
    if row["used_at"] is not None:
        return "used"
    if row["expires_at"] <= datetime.now(TZ).isoformat():
        return "expired"
    return "pending"
