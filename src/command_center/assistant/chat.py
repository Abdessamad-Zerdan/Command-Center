"""The assistant's core: answers questions grounded in your own data,
turns requests into concrete actions (tasks, calendar events, moving
items between days, projects, competitions, map cards), splits a long
brain dump into a reviewable batch, and carries out what you confirm.

The implementation is withheld from this public showcase — only the
surface the rest of the app calls into (see assistant/router.py) is
kept, to show how the pieces fit together. See README.md.
"""

from command_center.triage import TriageProviderError

_WITHHELD = "The assistant is withheld from the public showcase — see README.md."


def answer(question: str, history: list[dict] | None = None) -> dict:
    """Respond to one chat message. Returns one of: a grounded reply
    ({"answer", "sources"}), one action awaiting confirmation
    ({"pending_action"}), a batch of them ({"pending_batch"}), or a
    navigation ({"answer", "navigate"})."""
    raise TriageProviderError(_WITHHELD)


def confirm_action(pending_action: dict, confirmed: bool) -> dict:
    """Carry out — or cancel — one previously proposed action. Arguments
    are validated against the tool schema (tools.validate_args) first."""
    raise TriageProviderError(_WITHHELD)


def confirm_batch(tasks: list[dict], confirmed: bool) -> dict:
    """Carry out the checked items of a proposed batch; cancel the rest."""
    raise TriageProviderError(_WITHHELD)
