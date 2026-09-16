"""Grounded Q&A (Part 1) plus task tool-calling (Part 2), same entry
point. Read-only chunks/live-brief context is unchanged from Part 1;
tool-calling is an additional branch in answer() — a plain question
still gets a plain-text answer via the exact same path as before.

Live state (brief items, task ids) is fetched fresh on every call rather
than being ingested into the vector store — see ingest.py's docstring
for why.

Conversation history is threaded in from the browser (see `history`
below) but never persisted — "session-only" per the original scope means
kept in the tab's memory, not written to disk. Without it, "the
assistant asks a clarifying question, you answer it" wouldn't actually
work: each call would otherwise be a fresh, memory-less exchange, so a
reply like "next Friday" would arrive with no idea what it's answering.
"""

import logging
import re
from datetime import datetime

from command_center import auth, license_gate, queries, triage
from command_center.assistant import retrieval, tools
from command_center.config import LANES, PROFILE, TZ

logger = logging.getLogger(__name__)

TOP_K = 5

_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s+|$)")
_MIN_DUMP_LENGTH = 60  # chars — guards against 3 short clauses false-positiving


def _looks_like_brain_dump(question: str) -> bool:
    """Cheap local heuristic, zero Groq calls. A brain dump reads as
    multiple loose thoughts rather than a single question or request:
    either one-thought-per-line (also catches '-'/'*'/'1.'-prefixed
    lists for free, since those are just separate lines), or several
    sentence-like clauses packed into one paragraph. A short question
    ending in '?' with 1-2 segments never matches either branch. A
    single long run-on sentence with no line breaks or terminal
    punctuation also won't match — it falls through to the normal flow,
    a safe default (worst case: one more clarifying-question round-trip,
    same as today).
    """
    lines = [line.strip() for line in question.splitlines() if line.strip()]
    if len(lines) >= 3:
        return True

    if len(question) < _MIN_DUMP_LENGTH:
        return False

    segments = [s for s in _SENTENCE_SPLIT_RE.split(question) if s.strip()]
    return len(segments) >= 3


try:
    from command_center.prompts import (
        ASSISTANT_EXTRACTION_SYSTEM_PROMPT as EXTRACTION_SYSTEM_PROMPT,
    )
    from command_center.prompts import (
        ASSISTANT_SYSTEM_PROMPT_TEMPLATE as SYSTEM_PROMPT_TEMPLATE,
    )
except ImportError:
    # prompts.py is gitignored (see README) — a fresh clone falls back to
    # this bare-bones placeholder until you write your own.
    from command_center.prompts_example import (
        ASSISTANT_EXTRACTION_SYSTEM_PROMPT as EXTRACTION_SYSTEM_PROMPT,
    )
    from command_center.prompts_example import (
        ASSISTANT_SYSTEM_PROMPT_TEMPLATE as SYSTEM_PROMPT_TEMPLATE,
    )


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _live_brief_summary() -> str:
    brief = queries.get_brief(_today())
    if brief is None:
        return "No brief has been generated yet today."

    lines = []
    for lane, items in brief["lanes"].items():
        if not items:
            continue
        titles = ", ".join(item["title"] for item in items)
        lines.append(f"{lane}: {titles}")
    return "\n".join(lines) if lines else "No pending items today."


def _live_task_context() -> tuple[str, dict[str, str]]:
    """(display text for the prompt, id -> title lookup). Only
    google_tasks-sourced items have a real Google task id in source_id —
    that's what update_task/complete_task need, not our internal row id.
    """
    brief = queries.get_brief(_today())
    if brief is None:
        return "No tasks available.", {}

    task_items = [
        item
        for items in brief["lanes"].values()
        for item in items
        if item["source"] == "google_tasks"
    ]
    if not task_items:
        return "No tasks available.", {}

    title_lookup = {item["source_id"]: item["title"] for item in task_items}
    context_text = "\n".join(f"{item['source_id']}: {item['title']}" for item in task_items)
    return context_text, title_lookup


_HISTORY_CONTEXT_DAYS = 7


def _recent_history_context() -> tuple[str, dict[int, str]]:
    """(display text for the prompt, item id -> title lookup) — lets the
    assistant answer "what was on yesterday's brief" and reference an
    item by its local id for move_task_to_date, without a separate
    lookup tool/round-trip. Scoped to the last _HISTORY_CONTEXT_DAYS days
    before today, pending items only — same "still open" definition
    get_brief() itself uses. Keys are ints (items.id), distinct in kind
    from _live_task_context's string (Google task id) keys — the two
    lookups get merged into one dict by callers with no collision risk.
    """
    items = queries.list_recent_pending_items(_today(), days=_HISTORY_CONTEXT_DAYS)
    if not items:
        return f"No pending items from the last {_HISTORY_CONTEXT_DAYS} days.", {}

    title_lookup = {item["id"]: item["title"] for item in items}
    context_text = "\n".join(
        f"{item['id']} ({item['brief_date']}, {item['lane']}): {item['title']}" for item in items
    )
    return context_text, title_lookup


def _project_context() -> tuple[str, dict[int, str]]:
    """(display text for the prompt, id -> name lookup) — same shape as
    _live_task_context, so create_task's project_id can reference a real
    registered project's id rather than a guessed/invented one."""
    projects = queries.list_registered_projects(active_only=True)
    if not projects:
        return "No registered projects.", {}
    project_lookup = {p["id"]: p["name"] for p in projects}
    context_text = "\n".join(f"{p['id']}: {p['name']}" for p in projects)
    return context_text, project_lookup


def _extract_tasks(question: str) -> list[dict]:
    """Runs extraction through the same Groq call path as single-action
    tool calls (triage.run_groq_chat_with_tools), scoped to only the
    extract_tasks schema — deliberately not the full context block
    (retrieved chunks, live brief, live task list) answer() normally
    injects, since extraction only needs the raw dump text plus a
    today's-date instruction for relative-date resolution. Returns a
    list of {"title": str, "due_date": str|None, "notes": str|None}
    dicts — already shaped as valid create_task args, so each one can be
    handed straight to tools.describe_pending/tools.dispatch without
    translation. Malformed or missing data degrades to dropping that
    item, or an empty list — never raises.
    """
    today = datetime.now(TZ)
    user_content = (
        f"Today's date is {today.strftime('%A, %Y-%m-%d')}. Resolve relative "
        f"dates ('next Friday', 'tomorrow') against this, not your training "
        f"data — use it to compute due_date in YYYY-MM-DD format.\n\n"
        f"Message:\n{question}"
    )
    messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    result = triage.run_groq_chat_with_tools(messages, tools=tools.EXTRACT_TASKS_SCHEMA)

    if not result["tool_calls"]:
        return []

    call = result["tool_calls"][0]
    if call["name"] != "extract_tasks" or not isinstance(call["arguments"], dict):
        return []

    raw_tasks = call["arguments"].get("tasks")
    if not isinstance(raw_tasks, list):
        return []

    extracted = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not title or not str(title).strip():
            continue
        extracted.append(
            {
                "title": str(title).strip(),
                "due_date": item.get("due_date") or None,
                "notes": item.get("notes") or None,
            }
        )
    return extracted


def _handle_view_brief(call: dict) -> dict:
    """view_brief is pure navigation — no mutation, so unlike every other
    tool it's executed here directly rather than becoming a
    pending_action the user has to confirm. Returning a "navigate" key
    is the frontend's cue to actually change the page; router.py's
    /assistant/ask just spreads whatever this returns straight into the
    JSON response, so no route change was needed for that to work.
    """
    args = call["arguments"]
    error = tools.validate_args("view_brief", args)
    if error:
        logger.info("Malformed view_brief call from Groq: %s (%s)", error, args)
        return {
            "answer": "I couldn't tell which day you meant — could you rephrase that?",
            "sources": [],
        }

    date = args["date"]
    if queries.get_brief(date) is None:
        return {"answer": f"There's no brief for {date}.", "sources": []}

    target = "/brief" if date == _today() else f"/history/{date}"
    return {"answer": f"Taking you to {date}.", "sources": [], "navigate": target}


def answer(question: str, history: list[dict] | None = None) -> dict:
    license_gate.require()  # see license_gate.py — a third, independent checkpoint
    if not history and _looks_like_brain_dump(question):
        extracted = _extract_tasks(question)

        if len(extracted) >= 2:
            for task in extracted:
                queries.log_tool_call("create_task", task, "proposed")
            return {
                "pending_batch": {
                    "tasks": [
                        {
                            "tool": "create_task",
                            "args": task,
                            "confirmation_text": tools.describe_pending("create_task", task, {}),
                        }
                        for task in extracted
                    ],
                }
            }

        if len(extracted) == 1:
            task = extracted[0]
            queries.log_tool_call("create_task", task, "proposed")
            return {
                "pending_action": {
                    "tool": "create_task",
                    "args": task,
                    "confirmation_text": tools.describe_pending("create_task", task, {}),
                }
            }

        # 0 extracted — fall through to the normal single-action flow
        # below, exactly as if the brain-dump branch had never fired.

    chunks = retrieval.top_k(question, k=TOP_K)
    context_text = "\n\n".join(f"[{c['section']}]\n{c['content']}" for c in chunks)
    live_state = _live_brief_summary()
    task_context, title_lookup = _live_task_context()
    project_context, project_lookup = _project_context()
    history_context, history_lookup = _recent_history_context()
    title_lookup = {**title_lookup, **history_lookup}  # int item ids, no key collision with string task ids

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(name=PROFILE["name"])
    today = datetime.now(TZ)
    user_content = (
        f"Today's date is {today.strftime('%A, %Y-%m-%d')}. Resolve relative "
        f"dates ('next Friday', 'tomorrow') against this, not your training "
        f"data — use it to compute due_date in YYYY-MM-DD format.\n\n"
        f"Context:\n{context_text}\n\n"
        f"Current tasks (today's brief):\n{live_state}\n\n"
        f"Tasks you can reference by id (for update_task/complete_task):\n{task_context}\n\n"
        f"Registered projects you can reference by id (for create_task's project_id):\n{project_context}\n\n"
        f"Recent past items you can reference by id (for move_task_to_date):\n{history_context}\n\n"
        f"Question: {question}"
    )

    messages = [{"role": "system", "content": system_prompt}]
    for turn in history or []:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_content})

    result = triage.run_groq_chat_with_tools(messages, tools=tools.TOOL_SCHEMAS)

    view_calls = [c for c in result["tool_calls"] if c["name"] == "view_brief"]
    if view_calls:
        # Handled before the generic single/multi-call branches below —
        # navigation never needs confirmation, so it must never become a
        # pending_action/pending_batch. If the model mixed it with other
        # calls in the same turn (an edge case), navigation wins; the
        # other calls are simply dropped rather than half-applied.
        return _handle_view_brief(view_calls[0])

    if len(result["tool_calls"]) == 1:
        call = result["tool_calls"][0]
        error = tools.validate_args(call["name"], call["arguments"])
        if error:
            logger.info("Malformed tool call from Groq: %s (%s)", call["name"], error)
            queries.log_tool_call(call["name"], call["arguments"] or {}, "proposed")
            return {
                "answer": "I couldn't quite work out what you wanted to change — could you rephrase that?",
                "sources": [],
            }

        queries.log_tool_call(call["name"], call["arguments"], "proposed")
        return {
            "pending_action": {
                "tool": call["name"],
                "args": call["arguments"],
                "confirmation_text": tools.describe_pending(
                    call["name"], call["arguments"], title_lookup, project_lookup
                ),
            }
        }

    if len(result["tool_calls"]) >= 2:
        # More than one proposed action in a single turn — e.g. "bring
        # all of yesterday's tasks to today" naturally produces one
        # move_task_to_date call per item. Reuses the exact pending_batch/
        # confirm_batch shape the brain-dump path already built, so
        # nothing new is needed on the frontend for this to work.
        proposed = []
        for call in result["tool_calls"]:
            error = tools.validate_args(call["name"], call["arguments"])
            if error:
                logger.info("Malformed tool call from Groq: %s (%s)", call["name"], error)
                continue
            queries.log_tool_call(call["name"], call["arguments"], "proposed")
            proposed.append(
                {
                    "tool": call["name"],
                    "args": call["arguments"],
                    "confirmation_text": tools.describe_pending(
                        call["name"], call["arguments"], title_lookup, project_lookup
                    ),
                }
            )
        if proposed:
            return {"pending_batch": {"tasks": proposed}}
        return {
            "answer": "I couldn't quite work out what you wanted to change — could you rephrase that?",
            "sources": [],
        }

    sources = list(dict.fromkeys(c["section"] for c in chunks))  # dedup, keep order
    return {"answer": result["content"] or "", "sources": sources}


_DEFAULT_CREATE_TASK_LANE = "action_items"


def _create_local_item_for_created_task(created_task_id: str | None, args: dict) -> None:
    """Inserts the local mirror row for a task the assistant just created
    in Google Tasks — directly, from the args already on hand, instead
    of triggering a full pipeline re-pull-and-retriage (an LLM call,
    easily several seconds) just to re-derive a lane/title the request
    already settled. Falls back to action_items when the user didn't
    state a lane — there's no triage pass left to auto-classify it, so
    this is the closest thing to a neutral default (urgent/meeting_prep/
    tasks_due all imply something the user didn't actually say). Skipped
    silently if Google didn't hand back an id — the real creation
    already succeeded either way.
    """
    if not created_task_id:
        return
    lane = args.get("lane")
    if lane not in LANES:
        lane = _DEFAULT_CREATE_TASK_LANE
    queries.create_synced_task_item(
        brief_date=_today(),
        lane=lane,
        title=args["title"],
        source_id=created_task_id,
        due_date=args.get("due_date"),
        project_id=tools.coerce_project_id(args.get("project_id")),
    )


def _sync_local_task_state(tool_name: str, args: dict, result: dict | None) -> None:
    """Keeps /brief and the assistant's own context in sync with what
    just happened in Google Tasks — otherwise a newly created task
    wouldn't appear until the next scheduled pull (up to an hour later),
    and an update/complete wouldn't be reflected at all (a re-pull uses
    INSERT OR IGNORE, so it never touches an already-existing row).
    Best-effort: the real Google Tasks change already succeeded by the
    time this runs, so a sync failure here is logged, not surfaced.
    """
    try:
        if tool_name == "create_task":
            _create_local_item_for_created_task((result or {}).get("id"), args)
        elif tool_name == "update_task" and args.get("title"):
            queries.update_item_from_task(args["task_id"], title=args["title"])
        elif tool_name == "complete_task":
            queries.update_item_from_task(args["task_id"], status="done")
    except Exception:
        logger.exception("Failed to sync local task state after %s", tool_name)


_LOCAL_ONLY_TOOLS = {"move_task_to_date"}  # no Google Tasks call involved — see dispatch()


def confirm_action(pending_action: dict, confirmed: bool) -> dict:
    tool_name = pending_action.get("tool")
    args = pending_action.get("args") or {}

    if not confirmed:
        queries.log_tool_call(tool_name, args, "cancelled")
        return {"answer": "Okay, not making that change.", "sources": []}

    # Resolve lookups before dispatch, not after — for complete_task the
    # sync flips the item to status='done' (dropping it out of
    # get_brief()'s pending-only results), and for move_task_to_date the
    # mutation itself moves the item's brief_date out of
    # _recent_history_context()'s window. Either way, a lookup taken
    # afterwards would miss the very item just acted on and describe_done
    # would fall back to the raw id.
    _, title_lookup = _live_task_context()
    _, project_lookup = _project_context()
    _, history_lookup = _recent_history_context()
    title_lookup = {**title_lookup, **history_lookup}

    try:
        credentials = None if tool_name in _LOCAL_ONLY_TOOLS else auth.get_google_credentials()
        result = tools.dispatch(tool_name, args, credentials)
    except auth.AuthNotConfigured as exc:
        queries.log_tool_call(tool_name, args, "failed")
        return {"answer": str(exc), "sources": []}
    except Exception:
        logger.exception("Tool call failed: %s", tool_name)
        queries.log_tool_call(tool_name, args, "failed")
        message = (
            "Couldn't make that change — try again in a moment."
            if tool_name in _LOCAL_ONLY_TOOLS
            else "Couldn't reach Google Tasks — try again in a moment."
        )
        return {"answer": message, "sources": []}

    queries.log_tool_call(tool_name, args, "confirmed")
    _sync_local_task_state(tool_name, args, result)
    return {
        "answer": tools.describe_done(tool_name, args, title_lookup, project_lookup),
        "sources": [],
    }


def confirm_batch(tasks: list[dict], confirmed: bool) -> dict:
    """Batch counterpart to confirm_action — loops over the existing
    tools.dispatch("create_task", ...) rather than a new bulk-create
    function. The frontend always sends the full original candidate
    list (checked and unchecked alike), so every item proposed in
    answer()'s batch branch gets exactly one terminal-status row here —
    nothing is left permanently "proposed".
    """
    if not confirmed:
        for task in tasks:
            queries.log_tool_call(task.get("tool") or "create_task", task.get("args") or {}, "cancelled")
        return {"answer": "Okay, not creating those tasks.", "sources": []}

    checked = [t for t in tasks if t.get("checked")]
    unchecked = [t for t in tasks if not t.get("checked")]
    for task in unchecked:
        queries.log_tool_call(task.get("tool") or "create_task", task.get("args") or {}, "cancelled")

    if not checked:
        return {"answer": "Okay, not creating those tasks.", "sources": []}

    # move_task_to_date needs no Google credentials — see dispatch()'s
    # docstring for that branch. Skip the fetch entirely if that's all
    # this batch contains, rather than failing a purely-local batch just
    # because Google isn't connected.
    needs_google = any((t.get("tool") or "create_task") not in _LOCAL_ONLY_TOOLS for t in checked)
    credentials = None
    if needs_google:
        try:
            credentials = auth.get_google_credentials()
        except auth.AuthNotConfigured as exc:
            for task in checked:
                queries.log_tool_call(task.get("tool") or "create_task", task.get("args") or {}, "failed")
            return {"answer": str(exc), "sources": []}

    # Resolved once, before any dispatch — same before-not-after reasoning
    # as confirm_action (a move/complete changes what these lookups would
    # themselves return afterward).
    _, title_lookup = _live_task_context()
    _, project_lookup = _project_context()
    _, history_lookup = _recent_history_context()
    title_lookup = {**title_lookup, **history_lookup}

    created_titles: list[str] = []
    failed_titles: list[str] = []
    other_done: list[str] = []
    other_failed: list[str] = []

    for task in checked:
        args = task.get("args") or {}
        tool_name = task.get("tool") or "create_task"
        try:
            result = tools.dispatch(tool_name, args, credentials)
        except Exception:
            logger.exception("Batch tool call failed: %s", tool_name)
            queries.log_tool_call(tool_name, args, "failed")
            if tool_name == "create_task":
                failed_titles.append(args.get("title") or "untitled task")
            else:
                other_failed.append(tools.describe_pending(tool_name, args, title_lookup, project_lookup))
            continue
        queries.log_tool_call(tool_name, args, "confirmed")
        if tool_name == "create_task":
            _create_local_item_for_created_task((result or {}).get("id"), args)
            created_titles.append(args.get("title") or "untitled task")
        elif tool_name == "update_task" and args.get("title"):
            queries.update_item_from_task(args["task_id"], title=args["title"])
            other_done.append(tools.describe_done(tool_name, args, title_lookup, project_lookup))
        elif tool_name == "complete_task":
            queries.update_item_from_task(args["task_id"], status="done")
            other_done.append(tools.describe_done(tool_name, args, title_lookup, project_lookup))
        else:
            # move_task_to_date already applied its own local mutation
            # inside dispatch() — nothing else to sync.
            other_done.append(tools.describe_done(tool_name, args, title_lookup, project_lookup))

    parts = []
    if created_titles:
        plural = "s" if len(created_titles) != 1 else ""
        parts.append(
            f"Created {len(created_titles)} task{plural}: " + ", ".join(f"'{t}'" for t in created_titles)
        )
    if failed_titles:
        parts.append(f"Couldn't create {len(failed_titles)}: " + ", ".join(f"'{t}'" for t in failed_titles))
    if other_done:
        parts.append(" ".join(other_done))
    if other_failed:
        parts.append("Couldn't make some changes: " + "; ".join(other_failed))
    return {"answer": " ".join(parts) or "Okay, not creating those tasks.", "sources": []}
