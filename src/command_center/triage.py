"""Triage: raw ingested items -> categorized, prioritized brief items.

Interchangeable providers behind one entry point, `run()` — the rest of
the app only ever calls that, never the provider internals. All are
structured-output only, never free-text parsing — Anthropic via forced
tool-use, Ollama/Groq via JSON-mode (see _run_ollama for why they differ
from Anthropic). TRIAGE_PROVIDER=auto is a fourth, composite mode: try
local Ollama first, fall back to Groq on any failure — see _run_auto.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta

import httpx
import openai

from command_center.config import GROQ_API_KEY, OLLAMA_MODEL, TRIAGE_MODEL, TRIAGE_PROVIDER, TZ
from command_center.sources import RawItem

try:
    from command_center.prompts import TRIAGE_SYSTEM_PROMPT as SYSTEM_PROMPT
except ImportError:
    # prompts.py is gitignored (see README) — a fresh clone falls back to
    # this bare-bones placeholder until you write your own.
    from command_center.prompts_example import TRIAGE_SYSTEM_PROMPT as SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class TriageProviderError(Exception):
    """Raised when a provider can't be reached or misbehaves, with an
    actionable message instead of a raw SDK stack trace.
    """


class ProviderRateLimited(TriageProviderError):
    """The provider said "slow down". Callers that can fall back to the
    local model do; the rest show the message (see _call_groq_chat_completion)."""


_OLLAMA_BASE_URL = "http://localhost:11434"

TRIAGE_SCHEMA = {}  # withheld from the public showcase - see README.md

TRIAGE_TOOL_DESCRIPTION = ""  # withheld from the public showcase - see README.md

# llama3.1:8b-instruct-q4_K_M (and smaller local models generally) proved
# unreliable at *forced* tool-calling on this task once the payload was a
# real, moderately complex multi-item brief — it would describe the tool's
# schema back in prose instead of calling it. Plain JSON-mode output (no
# tools/tool_choice at all, just `response_format: json_object` plus an
# explicit shape description in the prompt) is what it reliably does
# instead — same model, same schema, different elicitation method. This
# is still fully structured, schema-validated output, not free-text
# parsing: json_object mode guarantees syntactically valid JSON back. Groq
# reuses this too — same OpenAI-compatible request shape either way, only
# the endpoint/key/model differ.


def _date_context(now: datetime | None = None) -> str:
    """Appended to every triage system prompt: the model has no clock, and
    without one it happily files a deadline that passed yesterday under
    "Tasks due — upload immediately"."""
    raise TriageProviderError("_date_context: withheld from the public showcase - see README.md")


def _language_instruction(language: str | None = None) -> str:
    """One output language for every card: without this, a Turkish mail
    got a Turkish summary next to English ones in the same lane."""
    raise TriageProviderError("_language_instruction: withheld from the public showcase - see README.md")


def _system_prompt() -> str:
    raise TriageProviderError("_system_prompt: withheld from the public showcase - see README.md")


def keep_omitted(items: list[dict], raw_items: list[RawItem]) -> list[dict]:
    """An item the model left out of its answer used to vanish from the
    brief without a trace — even an accepted meeting. Now it's kept: an
    attended calendar event in Meeting prep, anything else in Reading
    (with no invented advice). Appends to and returns `items`."""
    raise TriageProviderError("keep_omitted: withheld from the public showcase - see README.md")


def place_due_tasks(items: list[dict]) -> list[dict]:
    """A Google Task due today (or overdue) is what Tasks due means — a
    date fact, not a judgement, so it's not left to the model. Urgent is
    left alone. Mutates and returns `items`."""
    raise TriageProviderError("place_due_tasks: withheld from the public showcase - see README.md")


def _attending(raw: RawItem | None) -> bool:
    raise TriageProviderError("_attending: withheld from the public showcase - see README.md")


def enforce_meeting_prep(items: list[dict], raw_items: list[RawItem]) -> list[dict]:
    """Deterministic, after the model: Meeting prep is exactly the
    meetings the user attends (accepted, tentative or their own).
      - An attended calendar event the model filed as Reading or an
        Action item goes to Meeting prep (Urgent is left alone — an
        interview in an hour may well be urgent).
      - Anything else the model put in Meeting prep moves to where it
        belongs: an unanswered calendar invite or an emailed invite
        (.ics) → Action items (it needs a reply); any other email
        ("add to calendar" promos, event newsletters) → Reading; a Google
        Task → Tasks due if it's due today or earlier, else Action items.
    Mutates and returns `items`."""
    raise TriageProviderError("enforce_meeting_prep: withheld from the public showcase - see README.md")


def run(raw_items: list[RawItem], model=None) -> list[dict]:
    """Returns dicts shaped exactly like the `items` table columns
    (lane, source, source_id, title, why_it_matters, suggested_next_step,
    priority, deep_link) — the same shape fixtures.py already inserts.

    `model` replaces the configured provider with a callable taking the
    raw items and returning `items`-shaped dicts — only the offline eval
    (triage_eval.py) uses it, so it measures exactly this code path.
    """
    raise TriageProviderError("run: withheld from the public showcase - see README.md")


_AUTO_OLLAMA_MODEL = "llama3.2:3b"


def _ollama_has_model(model: str) -> bool:
    """Checks whether `model` is actually pulled locally — the sole
    signal auto mode uses to decide Ollama vs Groq, no guessing at
    what else might be installed. Returns False (fall back to Groq
    without even attempting Ollama) if the daemon isn't reachable
    either.
    """
    try:
        response = httpx.get(f"{_OLLAMA_BASE_URL}/api/tags", timeout=2.0)
        response.raise_for_status()
        models = response.json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return False
    return any(m.get("name") == model for m in models)


def _run_auto(raw_items: list[RawItem]) -> list[dict]:
    """Prefers local Ollama — free, private, no token usage — and falls
    back to Groq automatically whenever the target model (llama3.2:3b,
    or OLLAMA_MODEL if set to pin a different one) isn't pulled, Ollama
    isn't running, or a request to it fails for any other reason
    mid-request. Re-checked on every call rather than cached at
    startup, so it self-corrects if Ollama gets started/stopped later.

    Groq must still be configured (GROQ_API_KEY in .env) for the
    fallback to actually work — this mode doesn't remove that
    requirement, it just means you don't need Groq to be *reachable* on
    every single call when Ollama is up and doing the work for free.
    """
    raise TriageProviderError("_run_auto: withheld from the public showcase - see README.md")


def _build_payload(raw_items: list[RawItem]) -> list[dict]:
    raise TriageProviderError("_build_payload: withheld from the public showcase - see README.md")


def _parse_due_date(due: str | None) -> str | None:
    """Google's `due` is RFC3339 ("2026-08-20T00:00:00.000Z"), no
    meaningful time-of-day. Returns plain YYYY-MM-DD, or None on
    missing/unparseable — never raises, same convention as
    assistant/tools.py::_format_due_date."""
    if not due:
        return None
    try:
        return datetime.fromisoformat(due.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def item_from_raw(
    raw: RawItem,
    lane: str,
    why_it_matters: str = "",
    suggested_next_step: str = "",
    priority: int = 3,
    deadline: str | None = None,
) -> dict:
    """A brief item (the `items` table's shape) for one raw item — used for
    the LLM's results and for items a rule decided without the LLM."""
    return {
        "lane": lane,
        "source": raw.source,
        "source_id": raw.source_id,
        "title": raw.title,
        "why_it_matters": why_it_matters,
        "suggested_next_step": suggested_next_step,
        "priority": priority,
        "deep_link": raw.metadata.get("deep_link", ""),
        "due_date": _parse_due_date(raw.metadata.get("due")),
        "source_time": raw.metadata.get("received_at") or None,
        "deadline": deadline or None,
    }


def _map_results(raw_items: list[RawItem], result_items: list[dict]) -> list[dict]:
    raise TriageProviderError("_map_results: withheld from the public showcase - see README.md")


def _run_anthropic(raw_items: list[RawItem]) -> list[dict]:
    raise TriageProviderError("_run_anthropic: withheld from the public showcase - see README.md")


# Empirically, llama3.1:8b-instruct-q4_K_M reliably follows the {"items": [...]}
# schema at this batch size (verified against a real 16-item inbox — small
# synthetic batches worked, the full batch made the model abandon the schema
# and echo the input back under the wrong key instead of triaging it). Batching
# trades a few extra local calls (cheap, no API cost) for schema adherence.

# Groq's free-tier TPM (tokens-per-minute) limit rejected a real single-shot
# request outright: "tokens per minute (TPM) Limit 12000, Requested 13106"
# against a full day's inbox — the model itself handles a large batch fine,
# it's the token-bucket rate limiter that can't. Splitting into smaller
# sequential requests keeps each one (and the rolling total) under the cap.


def _run_ollama(raw_items: list[RawItem], model: str | None = None) -> list[dict]:
    # `model` is resolved here (not a `model: str = TRIAGE_MODEL` default
    # argument) so a test/caller monkeypatching triage.TRIAGE_MODEL still
    # takes effect — default-argument expressions bind once at import
    # time, before any monkeypatch can run.
    raise TriageProviderError("_run_ollama: withheld from the public showcase - see README.md")


def _run_groq(raw_items: list[RawItem]) -> list[dict]:
    raise TriageProviderError("_run_groq: withheld from the public showcase - see README.md")


def _run_batched(
    chat_fn,
    raw_items: list[RawItem],
    model: str,
    provider_label: str,
    batch_size: int,
) -> list[dict]:
    """Shared by Ollama and Groq — both need item-count batching, just for
    different reasons (schema adherence vs a TPM rate limit). One batch
    failing is logged and skipped, not fatal to the rest; only raises if
    every batch failed, in which case it re-raises the single real error
    rather than a summary (common case: one batch total).
    """
    raise TriageProviderError("_run_batched: withheld from the public showcase - see README.md")


def _call_chat_completion(
    client: openai.OpenAI,
    model: str,
    messages: list[dict],
    provider_label: str,
    **kwargs,
):
    """Connection + error-mapping shared by every OpenAI-compatible chat-
    completions call in this app (Ollama/Groq triage batches, and the
    assistant's free-text Groq chat in run_groq_chat) — only the request
    shape (JSON-mode + batch payload vs plain messages) differs per
    caller. Anthropic stays fully separate (see _run_anthropic) — its
    message/tool format is genuinely different, not just a different
    endpoint.
    """
    try:
        return client.chat.completions.create(model=model, messages=messages, **kwargs)
    except openai.AuthenticationError as exc:
        raise TriageProviderError(
            f"{provider_label} request failed: authentication error ({exc}). "
            "Check the API key in .env."
        ) from exc
    except (openai.APIConnectionError, openai.NotFoundError) as exc:
        if provider_label == "Ollama":
            hint = f"Is `ollama serve` running, and have you run `ollama pull {model}`?"
        else:
            hint = f"Is the model name {model!r} correct for {provider_label}, and is it reachable?"
        raise TriageProviderError(f"{provider_label} request failed ({exc}). {hint}") from exc
    except openai.RateLimitError as exc:
        raise ProviderRateLimited(f"{provider_label} request failed: rate limit reached ({exc}).") from exc
    except openai.BadRequestError as exc:
        # Observed live with tool-calling: Groq's strict function-call
        # parser occasionally rejects the model's own output (a
        # malformed tool call, "tool_use_failed") even when the model's
        # reasoning was correct — a model-output-formatting flake, not a
        # config problem. Degrades the same as every other provider
        # failure here rather than propagating an uncaught 500.
        raise TriageProviderError(f"{provider_label} request failed: {exc}.") from exc


_GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Groq's rate-limit error messages include a live "try again in Xm Ys"
# countdown — parsed here instead of guessing, so calls resume exactly
# when Groq says they can.
_RETRY_AFTER_PATTERN = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", re.IGNORECASE)

# A rate limit that clears within this is simply waited out, once.
_SHORT_WAIT_SECONDS = 10.0
# When Groq gives no countdown.
_DEFAULT_COOLDOWN_SECONDS = 60.0

# Until when Groq has asked us to stay away (TZ-aware), or None. Per
# process, in memory: while it's set, Groq isn't called at all, so a busy
# pull doesn't keep hammering a rate limit it already hit.
_groq_paused_until: datetime | None = None


def _parse_retry_after_seconds(message: str) -> float | None:
    match = _RETRY_AFTER_PATTERN.search(message)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    total = float(seconds)
    if minutes:
        total += int(minutes) * 60
    if hours:
        total += int(hours) * 3600
    return total


def _call_groq_chat_completion(model: str, messages: list[dict], provider_label: str = "Groq", **kwargs):
    """Every Groq call in this app (triage batches, report reflections, the
    assistant's chat and tool calls) goes through here — with one key,
    GROQ_API_KEY. Rotating across several accounts' keys to dodge rate
    limits (what this used to do) likely breaks Groq's terms, so instead:
    a rate limit that clears within _SHORT_WAIT_SECONDS is waited out once;
    a longer one pauses Groq for as long as it asked (no calls meanwhile)
    and raises ProviderRateLimited, which triage and the reflections answer
    by falling back to the local Ollama model (see _groq_or_local).
    """
    global _groq_paused_until
    if not GROQ_API_KEY:
        raise TriageProviderError(f"{provider_label} request needs GROQ_API_KEY set in .env.")
    now = datetime.now(TZ)
    if _groq_paused_until is not None and now < _groq_paused_until:
        remaining = (_groq_paused_until - now).total_seconds()
        raise ProviderRateLimited(f"{provider_label} is rate-limited — try again in {remaining:.0f}s.")

    client = openai.OpenAI(base_url=_GROQ_BASE_URL, api_key=GROQ_API_KEY)
    try:
        return _call_chat_completion(client, model, messages, provider_label=provider_label, **kwargs)
    except ProviderRateLimited as exc:
        wait = _parse_retry_after_seconds(str(exc)) or _DEFAULT_COOLDOWN_SECONDS
        if wait > _SHORT_WAIT_SECONDS:
            _groq_paused_until = datetime.now(TZ) + timedelta(seconds=wait)
            raise ProviderRateLimited(f"{provider_label} is rate-limited — try again in {wait:.0f}s.") from exc
        logger.info("%s rate-limited for %.1fs — waiting it out once", provider_label, wait)
        time.sleep(wait)
    try:
        return _call_chat_completion(client, model, messages, provider_label=provider_label, **kwargs)
    except ProviderRateLimited as exc:
        wait = _parse_retry_after_seconds(str(exc)) or _DEFAULT_COOLDOWN_SECONDS
        _groq_paused_until = datetime.now(TZ) + timedelta(seconds=wait)
        raise ProviderRateLimited(f"{provider_label} is rate-limited — try again in {wait:.0f}s.") from exc


def _local_fallback_model() -> str | None:
    """The Ollama model to fall back to on a Groq rate limit, if one is
    actually pulled locally — same check auto mode uses."""
    model = OLLAMA_MODEL or _AUTO_OLLAMA_MODEL
    return model if _ollama_has_model(model) else None


def _groq_or_local(messages: list[dict], model: str | None = None, **kwargs):
    """A Groq chat completion, or — while Groq is rate-limited — the same
    request to the local Ollama model, when there is one. Used where the
    request shape works on a small local model (JSON-mode triage, free-text
    reflections); tool-calling stays Groq-only and shows the rate limit."""
    try:
        return _call_groq_chat_completion(model or TRIAGE_MODEL, messages, provider_label="Groq", **kwargs)
    except ProviderRateLimited as exc:
        local = _local_fallback_model()
        if local is None:
            raise
        logger.warning("%s — using local Ollama model %r instead", exc, local)
        client = openai.OpenAI(base_url=f"{_OLLAMA_BASE_URL}/v1", api_key="ollama")
        return _call_chat_completion(client, local, messages, provider_label="Ollama", **kwargs)


def _run_openai_compatible_batch(
    chat_fn, batch: list[RawItem], model: str, provider_label: str
) -> list[dict]:
    """Shared by Ollama and Groq — both are OpenAI-compatible chat-completions
    endpoints using the same request/response shape; chat_fn carries
    whatever connection/retry behavior the caller needs (a plain client
    call for Ollama, primary+backup-key fallback for Groq).
    """
    raise TriageProviderError("_run_openai_compatible_batch: withheld from the public showcase - see README.md")


def run_groq_chat(messages: list[dict], max_tokens: int = 1024) -> str:
    """Free-text Groq chat completion — used by the assistant's grounded
    Q&A, not triage's structured JSON-mode output. Shares connection,
    error-handling, and primary/backup-key fallback with the triage path
    via _call_groq_chat_completion rather than standing up its own client.
    """
    response = _groq_or_local(messages, max_tokens=max_tokens)

    content = response.choices[0].message.content
    if not content:
        raise TriageProviderError(f"Groq model {TRIAGE_MODEL!r} returned an empty response.")
    return content


def run_groq_chat_with_tools(
    messages: list[dict], tools: list[dict], max_tokens: int = 1024
) -> dict:
    """Like run_groq_chat, but with tools attached and tool_choice="auto"
    (never forced — a plain question must still get a plain-text answer,
    not a coerced tool call). Returns
    {"content": str | None, "tool_calls": [{"name": str, "arguments": dict | None}]}
    — "arguments" is None if the model's JSON didn't parse, which the
    caller treats as a malformed call, not a crash.
    """
    response = _call_groq_chat_completion(
        TRIAGE_MODEL,
        messages,
        max_tokens=max_tokens,
        tools=tools,
        tool_choice="auto",
    )

    message = response.choices[0].message
    tool_calls = []
    for call in message.tool_calls or []:
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            arguments = None
        tool_calls.append({"name": call.function.name, "arguments": arguments})

    return {"content": message.content, "tool_calls": tool_calls}


def _parse_items_json(raw_content: str, provider_label: str) -> list[dict]:
    """Parses the `items` array out of a JSON-mode response. Defends
    against the (still occasionally observed) case where a local model
    double-encodes `items` as a JSON string instead of a native array.
    """
    raise TriageProviderError("_parse_items_json: withheld from the public showcase - see README.md")
