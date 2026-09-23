"""Task tool-calling: Groq function schemas, arg validation, dispatch to
the real Google Tasks functions, and human-readable confirmation/result
text. Every call targets Google's "@default" task list — see the Part 2
plan for why (most accounts only ever have one list; the alternative is
threading a real tasklist_id through triage/queries/db, deferred until
it's actually needed).
"""

from datetime import datetime, timedelta

from command_center import queries
from command_center.config import LANES, TZ
from command_center.sources import tasks as tasks_module
from command_center.sources.calendar import CalendarSource

DEFAULT_TASKLIST_ID = "@default"

TOOL_NAMES = {
    "create_task",
    "update_task",
    "complete_task",
    "move_task_to_date",
    "view_brief",
    "create_calendar_event",
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "Create a new task in the user's Google Tasks. Use only "
                "when the user clearly asks to add or create a task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The task's title, exactly as the user described it.",
                    },
                    "due_date": {
                        "type": "string",
                        "description": (
                            "Due date in YYYY-MM-DD format. Omit entirely "
                            "if no date was mentioned."
                        ),
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional extra detail for the task. Omit if not mentioned.",
                    },
                    "lane": {
                        "type": "string",
                        "enum": list(LANES),
                        "description": (
                            "Only set this if the user explicitly stated urgency or "
                            "named a lane directly (e.g. 'this is urgent', 'asap', "
                            "'add it to my meeting prep', 'it's not that important'). "
                            "Omit entirely if they didn't say anything about "
                            "urgency/category — it'll be classified automatically."
                        ),
                    },
                    "project_id": {
                        "type": "integer",
                        "description": (
                            "Only set this if the user said the task relates to one "
                            "of the registered projects listed in context below, "
                            "copying its exact numeric id. Never invent an id or "
                            "guess from a name that isn't in that list. Omit "
                            "entirely if no project was mentioned."
                        ),
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": (
                "Update an existing task's title, due date, or notes. "
                "Requires the task's id, copied exactly from the task "
                "list given in context. Never invent an id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The id of the task to update, from the task list in context.",
                    },
                    "title": {
                        "type": "string",
                        "description": "New title, if changing it.",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "New due date in YYYY-MM-DD format, if changing it.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "New notes, if changing them.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": (
                "Mark an existing task as done. Requires the task's id, "
                "copied exactly from the task list given in context. "
                "Never invent an id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The id of the task to mark complete, from the task list in context.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_task_to_date",
            "description": (
                "Move an item from a past day's brief to a different day "
                "(usually today). Requires the item's local id, copied "
                "exactly from the recent items list given in context — "
                "that is NOT the same kind of id as update_task/"
                "complete_task's task_id (a Google Tasks id). Never "
                "invent an id; if you can't tell which item the user "
                "means, ask a clarifying question instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "integer",
                        "description": "The item's local id, from the recent items list in context.",
                    },
                    "target_date": {
                        "type": "string",
                        "description": (
                            "The day to move it to, in YYYY-MM-DD format. Resolve "
                            "relative dates ('today', 'tomorrow') against today's "
                            "date given in context."
                        ),
                    },
                    "lane": {
                        "type": "string",
                        "enum": list(LANES),
                        "description": (
                            "Only set this if the user explicitly asked to also "
                            "change its lane/category (e.g. 'move it to Tasks "
                            "Due'). Omit to keep its current lane."
                        ),
                    },
                },
                "required": ["item_id", "target_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": (
                "Create a new event on the user's Google Calendar. Use "
                "only when the user clearly asks to schedule, book, or "
                "add an event/meeting to their calendar — not for a "
                "task or to-do (use create_task for those instead)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The event's title, exactly as the user described it.",
                    },
                    "date": {
                        "type": "string",
                        "description": (
                            "The event's date in YYYY-MM-DD format. Resolve "
                            "relative dates ('tomorrow', 'next Friday') against "
                            "today's date given in context."
                        ),
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "Start time in 24-hour HH:MM format, if the user "
                            "gave a specific time. Omit entirely for an "
                            "all-day event."
                        ),
                    },
                    "end_time": {
                        "type": "string",
                        "description": (
                            "End time in 24-hour HH:MM format. Only set this "
                            "if the user gave an explicit end time or "
                            "duration; if they gave a start_time but no end, "
                            "omit this too — it defaults to one hour after "
                            "start_time."
                        ),
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional extra detail for the event description. Omit if not mentioned.",
                    },
                },
                "required": ["title", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_brief",
            "description": (
                "Navigate the user to the brief for a specific day — "
                "today's or a past day's. Use this when they ask to see, "
                "go to, pull up, or be taken to a day's brief or tasks "
                "(e.g. 'take me to yesterday's brief', 'show me last "
                "Tuesday', 'go to today'). This only navigates — it "
                "never changes anything, so don't ask for confirmation "
                "first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": (
                            "The day to show, in YYYY-MM-DD format. Resolve "
                            "relative dates ('yesterday', 'last Monday', "
                            "'today') against today's date given in context."
                        ),
                    },
                },
                "required": ["date"],
            },
        },
    },
]

# Passed to run_groq_chat_with_tools on its own (never merged into
# TOOL_SCHEMAS) — a brain-dump message should only ever produce an
# extract_tasks call or plain content, never get confused with the three
# real task-mutation tools in the same call. Each extracted item is
# already exactly create_task's args shape, so extraction results are
# handled directly in chat.py — never routed through dispatch()/
# validate_args()/TOOL_NAMES, which are shaped around single-item args,
# not an array payload.
EXTRACT_TASKS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "extract_tasks",
            "description": (
                "Extract a list of candidate tasks from a free-form brain "
                "dump message. Return one entry per distinct actionable "
                "item mentioned — do not merge unrelated items, and do "
                "not invent items the user didn't mention. If nothing in "
                "the message is actionable, return an empty list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "The candidate tasks found in the message, in the order mentioned.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "The task's title, exactly as the user described it.",
                                },
                                "due_date": {
                                    "type": "string",
                                    "description": (
                                        "Due date in YYYY-MM-DD format, if a date or "
                                        "relative date ('Friday', 'next week') was "
                                        "mentioned for this item. Omit entirely otherwise."
                                    ),
                                },
                                "notes": {
                                    "type": "string",
                                    "description": (
                                        "Any other detail mentioned for this item that "
                                        "isn't the title or due date. Omit if none."
                                    ),
                                },
                            },
                            "required": ["title"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    },
]


def _blank(value: object) -> bool:
    return value is None or not str(value).strip()


def _parses_as_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _parses_as_time(value: str) -> bool:
    try:
        datetime.strptime(value, "%H:%M")
        return True
    except ValueError:
        return False


def validate_args(name: str, args: dict | None) -> str | None:
    """Returns an error message, or None if args look usable."""
    if name not in TOOL_NAMES:
        return f"Unknown tool: {name!r}"
    if not isinstance(args, dict):
        return "Could not parse the tool call arguments."
    if name == "create_task" and _blank(args.get("title")):
        return "create_task requires a title."
    if name == "create_task" and args.get("lane") and args["lane"] not in LANES:
        return f"create_task's lane must be one of: {', '.join(LANES)}."
    if name in ("update_task", "complete_task") and _blank(args.get("task_id")):
        return f"{name} requires a task_id."
    if name == "move_task_to_date":
        if args.get("item_id") is None:
            return "move_task_to_date requires an item_id."
        if _blank(args.get("target_date")):
            return "move_task_to_date requires a target_date."
        elif not _parses_as_date(args["target_date"]):
            return "move_task_to_date's target_date must be in YYYY-MM-DD format."
        if args.get("lane") and args["lane"] not in LANES:
            return f"move_task_to_date's lane must be one of: {', '.join(LANES)}."
    if name == "view_brief":
        if _blank(args.get("date")):
            return "view_brief requires a date."
        elif not _parses_as_date(args["date"]):
            return "view_brief's date must be in YYYY-MM-DD format."
    if name == "create_calendar_event":
        if _blank(args.get("title")):
            return "create_calendar_event requires a title."
        if _blank(args.get("date")):
            return "create_calendar_event requires a date."
        elif not _parses_as_date(args["date"]):
            return "create_calendar_event's date must be in YYYY-MM-DD format."
        for field in ("start_time", "end_time"):
            value = args.get(field)
            if value and not _parses_as_time(value):
                return f"create_calendar_event's {field} must be in HH:MM (24-hour) format."
    return None


def _format_due_date(date_str: str | None) -> str | None:
    """Human-readable form for confirmation/result text. Falls back to
    the raw string if it doesn't parse — never crashes just to render a
    confirmation card."""
    if not date_str:
        return None
    try:
        date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return date_str
    return date.strftime("%A, %b %d")


def _to_rfc3339_date(date_str: str | None) -> str | None:
    """Google Tasks' `due` field wants RFC3339, not YYYY-MM-DD. Returns
    None (omit the field) if the model's date didn't parse, rather than
    sending garbage to the API."""
    if not date_str:
        return None
    try:
        date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    return date.strftime("%Y-%m-%dT00:00:00.000Z")


def _event_start_end(date_str: str, start_time: str | None, end_time: str | None) -> tuple[dict, dict]:
    """Builds the Calendar API's own start/end shape (see
    CalendarSource.create_event) from create_calendar_event's args — a
    timed event if start_time was given, defaulting end_time to one hour
    later when the model didn't set one (or set one that isn't actually
    after start_time); an all-day event otherwise, with the end date
    exclusive per Google's own all-day convention (same +1 day fallback
    calendar_sync.py already uses for a due_date-only item)."""
    if not start_time:
        end_date = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).date().isoformat()
        return {"date": date_str}, {"date": end_date}

    start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
    end_dt = None
    if end_time:
        end_dt = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M")
        if end_dt <= start_dt:
            end_dt = None
    if end_dt is None:
        end_dt = start_dt + timedelta(hours=1)

    tz_name = str(TZ)
    return (
        {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz_name},
        {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz_name},
    )


def dispatch(name: str, args: dict, credentials) -> dict:
    if name == "create_task":
        return tasks_module.create_task(
            credentials,
            tasklist_id=DEFAULT_TASKLIST_ID,
            title=args["title"],
            notes=args.get("notes") or "",
            due=_to_rfc3339_date(args.get("due_date")) or "",
        )
    if name == "update_task":
        return tasks_module.update_task(
            credentials,
            tasklist_id=DEFAULT_TASKLIST_ID,
            task_id=args["task_id"],
            title=args.get("title"),
            notes=args.get("notes"),
            due=_to_rfc3339_date(args.get("due_date")),
        )
    if name == "complete_task":
        return tasks_module.complete_task(
            credentials, tasklist_id=DEFAULT_TASKLIST_ID, task_id=args["task_id"]
        )
    if name == "move_task_to_date":
        # Local-DB-only — no Google Tasks API call, so `credentials` is
        # unused here (move_task_to_date is exempted from the auth fetch
        # in chat.py's confirm_action/confirm_batch for exactly this
        # reason). brief_date/lane have no equivalent on a Google Task.
        moved = queries.move_item_to_date(
            item_id=int(args["item_id"]),
            new_brief_date=args["target_date"],
            lane=args.get("lane"),
        )
        return {"moved": moved}
    if name == "create_calendar_event":
        start, end = _event_start_end(args["date"], args.get("start_time"), args.get("end_time"))
        event = CalendarSource(credentials).create_event(
            args["title"], start, end, description=args.get("notes") or ""
        )
        return {"id": event.get("id"), "link": event.get("htmlLink", "")}
    raise ValueError(f"Unknown tool: {name!r}")


def _resolve_title(args: dict, title_lookup: dict[str, str]) -> str:
    if args.get("task_id"):
        return title_lookup.get(args["task_id"], args["task_id"])
    return args.get("title") or "this task"


def coerce_project_id(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lane_and_project_clause(args: dict, project_lookup: dict[int, str] | None) -> str:
    """' — Urgent lane, related to Foo' style suffix — only for whichever
    of lane/project_id create_task's args actually set. This is the
    confirmation card's only way of showing what was detected before the
    user commits, so it has to reflect exactly what's in args, nothing
    inferred beyond it."""
    parts = []
    lane = args.get("lane")
    lane_labels = queries.get_lane_labels()
    if lane in lane_labels:
        parts.append(f"{lane_labels[lane]} lane")
    project_id = coerce_project_id(args.get("project_id"))
    if project_lookup and project_id in project_lookup:
        parts.append(f"related to {project_lookup[project_id]}")
    return " — " + ", ".join(parts) if parts else ""


def describe_pending(
    name: str,
    args: dict,
    title_lookup: dict[str, str],
    project_lookup: dict[int, str] | None = None,
) -> str:
    if name == "create_task":
        due = _format_due_date(args.get("due_date"))
        due_clause = f" due {due}" if due else ""
        extra = _lane_and_project_clause(args, project_lookup)
        return f"Create task '{args['title']}'{due_clause}{extra}?"

    if name == "update_task":
        current_title = _resolve_title(args, title_lookup)
        changes = []
        if args.get("title"):
            changes.append(f"title to '{args['title']}'")
        due = _format_due_date(args.get("due_date"))
        if due:
            changes.append(f"due date to {due}")
        if args.get("notes"):
            changes.append("notes")
        change_text = " and ".join(changes) if changes else "details"
        return f"Update '{current_title}' — set {change_text}?"

    if name == "complete_task":
        title = _resolve_title(args, title_lookup)
        return f"Mark '{title}' as complete?"

    if name == "move_task_to_date":
        title = title_lookup.get(args.get("item_id")) or f"item {args.get('item_id')}"
        target = _format_due_date(args.get("target_date")) or args.get("target_date")
        lane_labels = queries.get_lane_labels()
        lane_clause = f" to {lane_labels[args['lane']]}" if args.get("lane") in lane_labels else ""
        return f"Move '{title}' to {target}{lane_clause}?"

    if name == "create_calendar_event":
        when = _format_due_date(args.get("date")) or args.get("date")
        time_clause = f" at {args['start_time']}" if args.get("start_time") else ""
        if args.get("start_time") and args.get("end_time"):
            time_clause += f"–{args['end_time']}"
        return f"Schedule '{args['title']}' on {when}{time_clause}?"

    return "Make this change?"


def describe_done(
    name: str,
    args: dict,
    title_lookup: dict[str, str],
    project_lookup: dict[int, str] | None = None,
) -> str:
    if name == "create_task":
        due = _format_due_date(args.get("due_date"))
        due_clause = f", due {due}" if due else ""
        extra = _lane_and_project_clause(args, project_lookup)
        return f"Done — created '{args['title']}'{due_clause}{extra}."

    if name == "update_task":
        title = _resolve_title(args, title_lookup)
        return f"Done — updated '{title}'."

    if name == "complete_task":
        title = _resolve_title(args, title_lookup)
        return f"Done — marked '{title}' as complete."

    if name == "move_task_to_date":
        title = title_lookup.get(args.get("item_id")) or f"item {args.get('item_id')}"
        target = _format_due_date(args.get("target_date")) or args.get("target_date")
        return f"Done — moved '{title}' to {target}."

    if name == "create_calendar_event":
        when = _format_due_date(args.get("date")) or args.get("date")
        return f"Done — scheduled '{args['title']}' on {when}."

    return "Done."
