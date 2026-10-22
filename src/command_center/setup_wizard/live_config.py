"""Patches an already-imported config value everywhere it was copied.

`config.py`'s constants get re-imported by name into other modules
(`from command_center.config import GROQ_API_KEY` in triage.py, etc.) —
each of those is its own independent binding. Writing a new value to
.env or profile.py doesn't retroactively update those, so anything that
wants a change to take effect without a process restart has to patch
every copy explicitly. That's what this module is for.
"""

import os
from contextlib import contextmanager
from typing import Iterator

from command_center import auth, config, triage
from command_center.sources import medium

_DEPENDENT_MODULES = (triage, medium, auth)


def patch(key: str, value) -> None:
    """In-memory only — config.<key> and any dependent module's copy of
    the same name. Does not touch os.environ or any file."""
    setattr(config, key, value)
    for mod in _DEPENDENT_MODULES:
        if hasattr(mod, key):
            setattr(mod, key, value)


def apply(key: str, value) -> None:
    """Permanent: sets the process env var too, on top of everything
    patch() does. Call after the corresponding .env write."""
    os.environ[key] = "" if value is None else str(value)
    patch(key, value)


@contextmanager
def temporary_patch(key: str, value) -> Iterator[None]:
    """Patches for the duration of a `with` block, then restores the
    original values. Used for the setup wizard's live test calls, which
    must never let a draft value outlive the request — the real .env
    only gets written once the user finishes the wizard.
    """
    original_config = getattr(config, key, None)
    original_by_module = {
        mod: getattr(mod, key) for mod in _DEPENDENT_MODULES if hasattr(mod, key)
    }
    patch(key, value)
    try:
        yield
    finally:
        setattr(config, key, original_config)
        for mod, original_value in original_by_module.items():
            setattr(mod, key, original_value)
