"""Task tool-calling: Groq function schemas, arg validation, dispatch to
the real Google Tasks functions, and human-readable confirmation/result
text. Every call targets Google's "@default" task list — see the Part 2
plan for why (most accounts only ever have one list; the alternative is
threading a real tasklist_id through triage/queries/db, deferred until
it's actually needed).
"""

from datetime import datetime, timedelta

from command_center import belongs, queries
from command_center.config import LANES, TZ
from command_center.sources.calendar import CalendarSource
from command_center.triage import TriageProviderError

DEFAULT_TASKLIST_ID = "@default"

TOOL_NAMES = {
    "create_task",
    "update_task",
    "complete_task",
    "move_task_to_date",
    "view_brief",
    "create_calendar_event",
    "create_project",
    "create_competition",
    "create_map_card",
}

# Tools that create something tasks can belong to. All local (no Google
# call), and in a batch they run before any create_task, so "add a
# competition X and these tasks for it" links the tasks to the X that
# was just created rather than finding nothing.
CREATOR_TOOLS = ("create_project", "create_competition", "create_map_card")

COMPETITION_KINDS = ("hackathon", "competition", "fellowship", "other")
MAP_CARD_KINDS = ("research", "target", "hackathon")


TOOL_SCHEMAS = []  # withheld from the public showcase - see README.md

# Passed to run_groq_chat_with_tools on its own (never merged into
# TOOL_SCHEMAS) — a brain-dump message should only ever produce an
# extract_tasks call or plain content, never get confused with the real
# single-action tools in the same call. Each extracted item carries its
# own "kind" (task or event) rather than being assumed to always be a
# task — chat.py maps that to create_task's or create_calendar_event's
# args shape and routes it through the same validate_args()/dispatch()
# every other tool call goes through, unlike earlier versions of this
# schema where extraction results bypassed validation entirely.
EXTRACT_TASKS_SCHEMA = []  # withheld from the public showcase - see README.md


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
    if name == "create_project" and _blank(args.get("name")):
        return "create_project requires a name."
    if name == "create_competition":
        if _blank(args.get("title")):
            return "create_competition requires a title."
        if args.get("kind") and args["kind"] not in COMPETITION_KINDS:
            return f"create_competition's kind must be one of: {', '.join(COMPETITION_KINDS)}."
        if args.get("deadline") and not _parses_as_date(args["deadline"]):
            return "create_competition's deadline must be in YYYY-MM-DD format."
    if name == "create_map_card":
        if _blank(args.get("title")):
            return "create_map_card requires a title."
        if args.get("kind") not in MAP_CARD_KINDS:
            return f"create_map_card's kind must be one of: {', '.join(MAP_CARD_KINDS)}."
        if args.get("target_date") and not _parses_as_date(args["target_date"]):
            return "create_map_card's target_date must be in YYYY-MM-DD format."
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


def creator_display_name(name: str, args: dict) -> str:
    return str(args.get("name") if name == "create_project" else args.get("title") or "").strip()


def _create_link_target(name: str, args: dict) -> dict:
    """create_project / create_competition / create_map_card — local only.
    Each new thing also lands as a card on the first Map board, so it's
    visible (and clickable into its breakdown) right away."""
    raise TriageProviderError("_create_link_target: withheld from the public showcase - see README.md")


def dispatch(name: str, args: dict, credentials) -> dict:
    raise TriageProviderError("dispatch: withheld from the public showcase - see README.md")


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


def belongs_to_clause(args: dict, new_names: set[str] | None = None) -> str:
    """' → for Agenthon (Competition)' — what create_task's belongs_to
    will link the task to, resolved the same way confirm-time linking
    resolves it, so the confirmation card can't promise a link that
    won't happen."""
    name = args.get("belongs_to")
    if _blank(name):
        return ""
    if new_names and str(name).strip().lower() in new_names:
        return f" → for {str(name).strip()} (new)"
    target = belongs.resolve(name)
    if target is None:
        return f" (couldn't find '{str(name).strip()}', so it won't be linked)"
    return f" → for {target['name']} ({belongs.KIND_LABELS.get(target['kind'], target['kind'].title())})"


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


_CREATOR_NOUNS = {"create_project": "project", "create_competition": "competition"}


def _creator_noun(name: str, args: dict) -> str:
    if name == "create_map_card":
        return args.get("kind") or "card"
    if name == "create_competition" and args.get("kind") in ("hackathon", "fellowship"):
        return args["kind"]
    return _CREATOR_NOUNS[name]


def describe_pending(
    name: str,
    args: dict,
    title_lookup: dict[str, str],
    project_lookup: dict[int, str] | None = None,
    new_names: set[str] | None = None,
) -> str:
    if name == "create_task":
        due = _format_due_date(args.get("due_date"))
        due_clause = f" due {due}" if due else ""
        extra = _lane_and_project_clause(args, project_lookup)
        return f"Create task '{args['title']}'{due_clause}{extra}{belongs_to_clause(args, new_names)}?"

    if name in CREATOR_TOOLS:
        when = args.get("deadline") or args.get("target_date")
        when_clause = f" (by {_format_due_date(when)})" if when else ""
        return f"Add {_creator_noun(name, args)} '{creator_display_name(name, args)}'{when_clause} to your Map?"

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
    result: dict | None = None,
) -> str:
    if name == "create_task":
        due = _format_due_date(args.get("due_date"))
        due_clause = f", due {due}" if due else ""
        extra = _lane_and_project_clause(args, project_lookup)
        linked = (result or {}).get("linked_to")
        link_clause = f", linked to {linked}" if linked else ""
        return f"Done — created '{args['title']}'{due_clause}{extra}{link_clause}."

    if name in CREATOR_TOOLS:
        noun = _creator_noun(name, args)
        display = creator_display_name(name, args)
        if (result or {}).get("existed"):
            return f"'{display}' was already there, so I used the existing {noun}."
        return f"Added {noun} '{display}' — it's on your Map and has its own page."

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
