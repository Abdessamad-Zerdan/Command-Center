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
from datetime import datetime, timedelta

import anthropic
import httpx
import openai

from command_center import license_gate
from command_center.config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    GROQ_API_KEY,
    GROQ_API_KEY_BACKUP,
    GROQ_API_KEY_BACKUP_2,
    OLLAMA_MODEL,
    TRIAGE_LANES,
    TRIAGE_MODEL,
    TRIAGE_PROVIDER,
    TZ,
)
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


_OLLAMA_BASE_URL = "http://localhost:11434"

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "source_id": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "suggested_next_step": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 3},
                    "lane": {"type": "string", "enum": list(TRIAGE_LANES)},
                },
                "required": [
                    "source",
                    "source_id",
                    "why_it_matters",
                    "suggested_next_step",
                    "priority",
                    "lane",
                ],
            },
        },
    },
    "required": ["items"],
}

TRIAGE_TOOL_DESCRIPTION = "Submit the categorized, prioritized triage of today's items."

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
_JSON_MODE_INSTRUCTIONS = (
    "\n\nRespond with ONLY a single JSON object, no other text before or "
    "after it, matching exactly this shape:\n"
    '{"items": [{"source": string, "source_id": string, '
    '"why_it_matters": string, "suggested_next_step": string, '
    '"priority": integer 1-3, "lane": one of ' + "|".join(f'"{lane}"' for lane in TRIAGE_LANES)
    + "}]}\n"
    "Do not explain the schema. Do not add commentary before or after the "
    "JSON. Output the JSON object and nothing else."
)


def run(raw_items: list[RawItem]) -> list[dict]:
    """Returns dicts shaped exactly like the `items` table columns
    (lane, source, source_id, title, why_it_matters, suggested_next_step,
    priority, deep_link) — the same shape fixtures.py already inserts.
    """
    license_gate.require()  # see license_gate.py — a second, independent checkpoint
    if not raw_items:
        return []

    if TRIAGE_PROVIDER == "anthropic":
        return _run_anthropic(raw_items)
    if TRIAGE_PROVIDER == "ollama":
        return _run_ollama(raw_items)
    if TRIAGE_PROVIDER == "groq":
        return _run_groq(raw_items)
    if TRIAGE_PROVIDER == "auto":
        return _run_auto(raw_items)
    raise ValueError(f"Unknown TRIAGE_PROVIDER: {TRIAGE_PROVIDER!r}")


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
    model = OLLAMA_MODEL or _AUTO_OLLAMA_MODEL
    if not _ollama_has_model(model):
        logger.info("Ollama model %r not available locally — using Groq.", model)
        return _run_groq(raw_items)
    try:
        return _run_ollama(raw_items, model=model)
    except TriageProviderError as exc:
        logger.warning("Ollama unavailable or failed (%s) — falling back to Groq", exc)
        return _run_groq(raw_items)


def _build_payload(raw_items: list[RawItem]) -> list[dict]:
    return [
        {
            "source": ri.source,
            "source_id": ri.source_id,
            "title": ri.title,
            "body": ri.body,
            **ri.metadata,
        }
        for ri in raw_items
    ]


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


def _map_results(raw_items: list[RawItem], result_items: list[dict]) -> list[dict]:
    by_key = {(ri.source, ri.source_id): ri for ri in raw_items}

    triaged = []
    for entry in result_items:
        key = (entry.get("source"), entry.get("source_id"))
        raw = by_key.get(key)
        # Skip anything that doesn't map back to a real input item or lane —
        # this is a cross-API-boundary response, validate before trusting it.
        if raw is None or entry.get("lane") not in TRIAGE_LANES:
            continue
        priority = entry.get("priority", 3)
        priority = min(3, max(1, int(priority))) if isinstance(priority, int) else 3

        triaged.append(
            {
                "lane": entry["lane"],
                "source": raw.source,
                "source_id": raw.source_id,
                "title": raw.title,
                "why_it_matters": entry.get("why_it_matters") or "",
                "suggested_next_step": entry.get("suggested_next_step") or "",
                "priority": priority,
                "deep_link": raw.metadata.get("deep_link", ""),
                "due_date": _parse_due_date(raw.metadata.get("due")),
            }
        )
    return triaged


def _run_anthropic(raw_items: list[RawItem]) -> list[dict]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    payload = _build_payload(raw_items)

    # Error mapping mirrors _call_chat_completion's (below) — same
    # provider-failure-degrades-the-lane philosophy, just against the
    # anthropic SDK's own exception types instead of openai's, since
    # Anthropic's message/tool format is genuinely different, not just a
    # different endpoint (see _call_chat_completion's own docstring).
    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=[
                {
                    "name": "submit_triage",
                    "description": TRIAGE_TOOL_DESCRIPTION,
                    "input_schema": TRIAGE_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": "submit_triage"},
            messages=[
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}
            ],
        )
    except anthropic.AuthenticationError as exc:
        raise TriageProviderError(
            f"Anthropic request failed: authentication error ({exc}). Check ANTHROPIC_API_KEY in .env."
        ) from exc
    except (anthropic.APIConnectionError, anthropic.NotFoundError) as exc:
        raise TriageProviderError(
            f"Anthropic request failed ({exc}). Is the model name {ANTHROPIC_MODEL!r} correct?"
        ) from exc
    except anthropic.RateLimitError as exc:
        raise TriageProviderError(f"Anthropic request failed: rate limit reached ({exc}).") from exc
    except anthropic.BadRequestError as exc:
        raise TriageProviderError(f"Anthropic request failed: {exc}.") from exc

    usage = response.usage
    logger.info(
        "triage tokens (anthropic): input=%s output=%s", usage.input_tokens, usage.output_tokens
    )

    tool_use = next(block for block in response.content if block.type == "tool_use")
    result_items = tool_use.input.get("items", [])
    return _map_results(raw_items, result_items)


# Empirically, llama3.1:8b-instruct-q4_K_M reliably follows the {"items": [...]}
# schema at this batch size (verified against a real 16-item inbox — small
# synthetic batches worked, the full batch made the model abandon the schema
# and echo the input back under the wrong key instead of triaging it). Batching
# trades a few extra local calls (cheap, no API cost) for schema adherence.
_OLLAMA_BATCH_SIZE = 5

# Groq's free-tier TPM (tokens-per-minute) limit rejected a real single-shot
# request outright: "tokens per minute (TPM) Limit 12000, Requested 13106"
# against a full day's inbox — the model itself handles a large batch fine,
# it's the token-bucket rate limiter that can't. Splitting into smaller
# sequential requests keeps each one (and the rolling total) under the cap.
_GROQ_BATCH_SIZE = 5


def _run_ollama(raw_items: list[RawItem], model: str | None = None) -> list[dict]:
    # `model` is resolved here (not a `model: str = TRIAGE_MODEL` default
    # argument) so a test/caller monkeypatching triage.TRIAGE_MODEL still
    # takes effect — default-argument expressions bind once at import
    # time, before any monkeypatch can run.
    if model is None:
        model = TRIAGE_MODEL
    client = openai.OpenAI(base_url=f"{_OLLAMA_BASE_URL}/v1", api_key="ollama")
    chat_fn = lambda messages, **kw: _call_chat_completion(  # noqa: E731
        client, model, messages, provider_label="Ollama", **kw
    )
    return _run_batched(chat_fn, raw_items, model, "Ollama", _OLLAMA_BATCH_SIZE)


def _run_groq(raw_items: list[RawItem]) -> list[dict]:
    if not GROQ_API_KEY:
        raise TriageProviderError(
            "TRIAGE_PROVIDER=groq but GROQ_API_KEY is not set in .env."
        )

    chat_fn = lambda messages, **kw: _call_groq_chat_completion(  # noqa: E731
        TRIAGE_MODEL, messages, provider_label="Groq", **kw
    )
    return _run_batched(chat_fn, raw_items, TRIAGE_MODEL, "Groq", _GROQ_BATCH_SIZE)


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
    batches = [raw_items[i : i + batch_size] for i in range(0, len(raw_items), batch_size)]

    results: list[dict] = []
    errors: list[TriageProviderError] = []
    for batch in batches:
        try:
            results.extend(_run_openai_compatible_batch(chat_fn, batch, model, provider_label))
        except TriageProviderError as exc:
            logger.exception(
                "%s triage batch of %d item(s) failed, skipping it", provider_label, len(batch)
            )
            errors.append(exc)

    if batches and len(errors) == len(batches):
        if len(errors) == 1:
            raise errors[0]
        raise TriageProviderError(
            f"All {len(errors)} {provider_label} triage batches failed: "
            + "; ".join(str(e) for e in errors)
        )
    return results


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
        raise TriageProviderError(f"{provider_label} request failed: rate limit reached ({exc}).") from exc
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
# countdown — parsed here instead of hardcoding a guessed cooldown, so a
# key comes back into rotation exactly when Groq says it will, not
# sooner (wasted call) or later (needless downtime).
_RETRY_AFTER_PATTERN = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", re.IGNORECASE)

# Fallback cooldown when a failure isn't a parseable rate limit (auth,
# connection) — long enough not to hammer a key that's likely still
# broken on the very next call, short enough to recover on its own if
# the underlying problem (e.g. a momentarily-flaky connection) clears up
# without requiring an app restart.
_DEFAULT_KEY_COOLDOWN_SECONDS = 300.0

# key string -> local timestamp (TZ-aware, via config.TZ) after which
# it's worth trying again. In-memory only, per process — reset on every
# restart, an accepted tradeoff: the cost is at most one wasted call per
# key after a restart, not a correctness issue, since a real 429/401
# from Groq is still the ground truth either way.
_key_cooldowns: dict[str, datetime] = {}


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


def _groq_keys() -> list[str]:
    """Every configured Groq key, in preference order (primary first).
    A key only ever moves later in the *rotation order* per-call (via
    the cooldown sort below) — this list itself never reorders."""
    return [k for k in (GROQ_API_KEY, GROQ_API_KEY_BACKUP, GROQ_API_KEY_BACKUP_2) if k]


def _key_available(key: str) -> bool:
    until = _key_cooldowns.get(key)
    return until is None or datetime.now(TZ) >= until


def _mark_key_cooldown(key: str, seconds: float) -> None:
    _key_cooldowns[key] = datetime.now(TZ) + timedelta(seconds=seconds)


def _call_groq_chat_completion(model: str, messages: list[dict], provider_label: str = "Groq", **kwargs):
    """Every Groq call in this app (triage batches, the assistant's
    free-text chat, and its tool-calling chat) goes through here. Tries
    every configured Groq key (GROQ_API_KEY, then GROQ_API_KEY_BACKUP,
    then GROQ_API_KEY_BACKUP_2 — however many are actually set) in
    order, skipping any key still in its cooldown window from a prior
    failure on this process, and falling further down the list on any
    failure (rate limit, an auth error because a key was revoked, a
    connection hiccup) rather than stopping at the first one that works.
    Only raises once every configured key has been tried and failed.
    """
    keys = _groq_keys()
    if not keys:
        raise TriageProviderError(f"{provider_label} request needs GROQ_API_KEY set in .env.")

    # Stable sort: available keys keep their preference order (primary
    # first); unavailable ones sort after, soonest-to-recover first — so
    # a key still on a 20-minute cooldown isn't tried ahead of one with
    # 30 seconds left.
    ordered = sorted(
        keys,
        key=lambda k: (0, 0.0) if _key_available(k) else (1, _key_cooldowns[k].timestamp()),
    )

    last_exc: TriageProviderError | None = None
    for i, key in enumerate(ordered):
        client = openai.OpenAI(base_url=_GROQ_BASE_URL, api_key=key)
        try:
            return _call_chat_completion(client, model, messages, provider_label=provider_label, **kwargs)
        except TriageProviderError as exc:
            last_exc = exc
            cooldown = _parse_retry_after_seconds(str(exc)) or _DEFAULT_KEY_COOLDOWN_SECONDS
            _mark_key_cooldown(key, cooldown)
            if i < len(ordered) - 1:
                logger.warning(
                    "%s key %d/%d failed (%s), trying the next one",
                    provider_label, i + 1, len(ordered), exc,
                )
    raise last_exc


def _run_openai_compatible_batch(
    chat_fn, batch: list[RawItem], model: str, provider_label: str
) -> list[dict]:
    """Shared by Ollama and Groq — both are OpenAI-compatible chat-completions
    endpoints using the same request/response shape; chat_fn carries
    whatever connection/retry behavior the caller needs (a plain client
    call for Ollama, primary+backup-key fallback for Groq).
    """
    payload = _build_payload(batch)

    response = chat_fn(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT + _JSON_MODE_INSTRUCTIONS},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, default=str),
            },
        ],
        max_tokens=4096,
        response_format={"type": "json_object"},
    )

    usage = response.usage
    logger.info(
        "triage tokens (%s): input=%s output=%s",
        provider_label.lower(),
        usage.prompt_tokens,
        usage.completion_tokens,
    )

    message = response.choices[0].message
    if not message.content:
        raise TriageProviderError(
            f"{provider_label} model {model!r} returned an empty response for "
            "this triage request."
        )

    result_items = _parse_items_json(message.content, provider_label)
    return _map_results(batch, result_items)


def run_groq_chat(messages: list[dict], max_tokens: int = 1024) -> str:
    """Free-text Groq chat completion — used by the assistant's grounded
    Q&A, not triage's structured JSON-mode output. Shares connection,
    error-handling, and primary/backup-key fallback with the triage path
    via _call_groq_chat_completion rather than standing up its own client.
    """
    response = _call_groq_chat_completion(TRIAGE_MODEL, messages, max_tokens=max_tokens)

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
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        logger.error("%s returned unparseable JSON: %r", provider_label, raw_content[:300])
        raise TriageProviderError(
            f"{provider_label} returned malformed JSON for this triage request "
            "— see logs for what it said."
        ) from exc

    items = parsed.get("items", [])
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            logger.error(
                "%s returned unparseable nested items string: %r", provider_label, items[:300]
            )
            return []
    if not isinstance(items, list):
        logger.error("%s returned non-list items: %r", provider_label, items)
        return []
    return items
