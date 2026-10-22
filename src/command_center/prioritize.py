"""High / Medium / Low for every open task on the brief, judged by the model.

Triage sets a priority only for what it triages (mail, calendar, Google
Tasks), one item at a time and without seeing the rest of the day — and a
task the user adds by hand never gets one at all, so almost everything
sat at "Medium". After each pull, one call looks at all open work on
today's brief together (the action lanes, carried-over days included) and
sorts it into High / Medium / Low with a one-line reason each.

Tokens: the call only happens when the set of open tasks actually changed
since the last run (a fingerprint of ids, titles, lanes and dates in
app_settings), so a quiet pull every 15 minutes costs nothing. A priority
the user set themselves (priority_locked) is never sent or changed.
"""

import logging
from datetime import datetime

from command_center import triage
from command_center.triage import TriageProviderError

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = ""  # withheld from the public showcase - see README.md


def _prompt(now: datetime) -> str:
    raise TriageProviderError("_prompt: withheld from the public showcase - see README.md")


def _task_line(task: dict) -> dict:
    raise TriageProviderError("_task_line: withheld from the public showcase - see README.md")


def _parse(reply: str, known_ids: set[int]) -> list[tuple[int, int, str]]:
    raise TriageProviderError("_parse: withheld from the public showcase - see README.md")


def run(today: str | None = None, now: datetime | None = None) -> int:
    """Re-judges today's open tasks if they changed since the last run.
    Returns how many tasks it updated. Raises TriageProviderError when the
    model fails — the fingerprint isn't saved then, so the next pull tries
    again."""
    raise TriageProviderError("run: withheld from the public showcase - see README.md")
