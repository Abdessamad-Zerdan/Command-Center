import json

import anthropic
import httpx2
import openai
import pytest

from command_center import triage
from command_center.sources import RawItem


@pytest.fixture(autouse=True)
def _no_real_groq_backup_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """config.py loads the real .env at import time, so triage.GROQ_API_KEY_BACKUP
    and _BACKUP_2 hold whatever real backup keys are actually configured for
    this project — without this, every test in this file that only sets
    GROQ_API_KEY would silently also have real backup keys in rotation,
    breaking tests that assert "no fallback available" behavior. Tests that
    specifically exercise multi-key fallback override these explicitly.

    Also resets the module-level _key_cooldowns dict — it's process-lifetime
    state (see triage.py's comment on it), so without clearing it a cooldown
    recorded by one test would leak into and skew the next test's key
    ordering.
    """
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP", None)
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP_2", None)
    monkeypatch.setattr(triage, "_key_cooldowns", {})


def _raw_item(source_id: str = "m1") -> RawItem:
    return RawItem(
        source="gmail",
        source_id=source_id,
        title="Test subject",
        body="Body text",
        metadata={"deep_link": "https://mail.google.com/x"},
    )


def test_run_returns_empty_list_for_no_items(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "TRIAGE_PROVIDER", "ollama")
    assert triage.run([]) == []


def test_run_dispatches_to_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "TRIAGE_PROVIDER", "anthropic")
    monkeypatch.setattr(triage, "_run_anthropic", lambda items: ["anthropic-result"])
    monkeypatch.setattr(triage, "_run_ollama", lambda items: ["ollama-result"])
    assert triage.run([_raw_item()]) == ["anthropic-result"]


def test_run_dispatches_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "TRIAGE_PROVIDER", "ollama")
    monkeypatch.setattr(triage, "_run_anthropic", lambda items: ["anthropic-result"])
    monkeypatch.setattr(triage, "_run_ollama", lambda items: ["ollama-result"])
    assert triage.run([_raw_item()]) == ["ollama-result"]


def test_run_dispatches_to_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "TRIAGE_PROVIDER", "groq")
    monkeypatch.setattr(triage, "_run_groq", lambda items: ["groq-result"])
    assert triage.run([_raw_item()]) == ["groq-result"]


def test_run_dispatches_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "TRIAGE_PROVIDER", "auto")
    monkeypatch.setattr(triage, "_run_auto", lambda items: ["auto-result"])
    assert triage.run([_raw_item()]) == ["auto-result"]


def test_run_raises_on_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "TRIAGE_PROVIDER", "carrier-pigeon")
    with pytest.raises(ValueError, match="Unknown TRIAGE_PROVIDER"):
        triage.run([_raw_item()])


# --- TRIAGE_PROVIDER=auto: prefer local Ollama, fall back to Groq -----------


class _FakeHttpxResponse:
    def __init__(self, payload: dict, status_error: Exception | None = None) -> None:
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error

    def json(self) -> dict:
        return self._payload


def test_ollama_has_model_true_when_the_exact_tag_is_pulled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        triage.httpx,
        "get",
        lambda url, timeout: _FakeHttpxResponse(
            {"models": [{"name": "llama3.1:8b-instruct-q4_K_M"}, {"name": "llama3.2:3b"}]}
        ),
    )
    assert triage._ollama_has_model("llama3.2:3b") is True


def test_ollama_has_model_false_when_a_different_tag_is_pulled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        triage.httpx,
        "get",
        lambda url, timeout: _FakeHttpxResponse({"models": [{"name": "llama3.1:8b-instruct-q4_K_M"}]}),
    )
    assert triage._ollama_has_model("llama3.2:3b") is False


def test_ollama_has_model_false_when_nothing_pulled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage.httpx, "get", lambda url, timeout: _FakeHttpxResponse({"models": []}))
    assert triage._ollama_has_model("llama3.2:3b") is False


def test_ollama_has_model_false_when_ollama_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(url, timeout):
        raise triage.httpx.ConnectError("connection refused")

    monkeypatch.setattr(triage.httpx, "get", _raise)
    assert triage._ollama_has_model("llama3.2:3b") is False


def test_run_auto_uses_ollama_when_the_default_model_is_pulled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "OLLAMA_MODEL", None)
    monkeypatch.setattr(triage, "_ollama_has_model", lambda model: model == "llama3.2:3b")
    captured = {}

    def _fake_run_ollama(items, model=None):
        captured["model"] = model
        return ["ollama-result"]

    monkeypatch.setattr(triage, "_run_ollama", _fake_run_ollama)
    monkeypatch.setattr(
        triage, "_run_groq", lambda items: (_ for _ in ()).throw(AssertionError("should not call Groq"))
    )

    assert triage._run_auto([_raw_item()]) == ["ollama-result"]
    assert captured["model"] == "llama3.2:3b"


def test_run_auto_checks_the_pinned_ollama_model_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "OLLAMA_MODEL", "pinned-model")
    checked = []
    monkeypatch.setattr(triage, "_ollama_has_model", lambda model: checked.append(model) or True)
    monkeypatch.setattr(triage, "_run_ollama", lambda items, model=None: ["ollama-result"])

    assert triage._run_auto([_raw_item()]) == ["ollama-result"]
    assert checked == ["pinned-model"]


def test_run_auto_falls_back_to_groq_when_default_model_is_not_pulled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(triage, "OLLAMA_MODEL", None)
    monkeypatch.setattr(triage, "_ollama_has_model", lambda model: False)
    monkeypatch.setattr(
        triage,
        "_run_ollama",
        lambda items, model=None: (_ for _ in ()).throw(AssertionError("should not call Ollama")),
    )
    monkeypatch.setattr(triage, "_run_groq", lambda items: ["groq-result"])

    assert triage._run_auto([_raw_item()]) == ["groq-result"]


def test_run_auto_falls_back_to_groq_when_ollama_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "OLLAMA_MODEL", None)
    monkeypatch.setattr(triage, "_ollama_has_model", lambda model: True)

    def _fail(items, model=None):
        raise triage.TriageProviderError("Ollama request failed (connection refused).")

    monkeypatch.setattr(triage, "_run_ollama", _fail)
    monkeypatch.setattr(triage, "_run_groq", lambda items: ["groq-result"])

    assert triage._run_auto([_raw_item()]) == ["groq-result"]


def test_map_results_maps_valid_entry() -> None:
    raw = _raw_item()
    result = triage._map_results(
        [raw],
        [
            {
                "source": "gmail",
                "source_id": "m1",
                "why_it_matters": "matters",
                "suggested_next_step": "do it",
                "priority": 1,
                "lane": "urgent",
            }
        ],
    )
    assert result == [
        {
            "lane": "urgent",
            "source": "gmail",
            "source_id": "m1",
            "title": "Test subject",
            "why_it_matters": "matters",
            "suggested_next_step": "do it",
            "priority": 1,
            "deep_link": "https://mail.google.com/x",
            "due_date": None,
        }
    ]


def test_map_results_includes_due_date_from_metadata() -> None:
    raw = RawItem(
        source="google_tasks",
        source_id="t1",
        title="Renew passport",
        body="",
        metadata={"deep_link": "", "due": "2026-08-20T00:00:00.000Z"},
    )
    result = triage._map_results(
        [raw],
        [
            {
                "source": "google_tasks",
                "source_id": "t1",
                "why_it_matters": "x",
                "suggested_next_step": "y",
                "priority": 2,
                "lane": "tasks_due",
            }
        ],
    )
    assert result[0]["due_date"] == "2026-08-20"


def test_parse_due_date_valid_rfc3339() -> None:
    assert triage._parse_due_date("2026-08-20T00:00:00.000Z") == "2026-08-20"


def test_parse_due_date_missing() -> None:
    assert triage._parse_due_date(None) is None
    assert triage._parse_due_date("") is None


def test_parse_due_date_malformed_never_raises() -> None:
    assert triage._parse_due_date("not-a-date") is None


def test_map_results_skips_unknown_source_id() -> None:
    raw = _raw_item()
    result = triage._map_results(
        [raw],
        [
            {
                "source": "gmail",
                "source_id": "does-not-exist",
                "why_it_matters": "x",
                "suggested_next_step": "y",
                "priority": 1,
                "lane": "urgent",
            }
        ],
    )
    assert result == []


def test_map_results_skips_invalid_lane() -> None:
    raw = _raw_item()
    result = triage._map_results(
        [raw],
        [
            {
                "source": "gmail",
                "source_id": "m1",
                "why_it_matters": "x",
                "suggested_next_step": "y",
                "priority": 1,
                "lane": "not_a_real_lane",
            }
        ],
    )
    assert result == []


def test_map_results_clamps_priority() -> None:
    raw = _raw_item()
    result = triage._map_results(
        [raw],
        [
            {
                "source": "gmail",
                "source_id": "m1",
                "why_it_matters": "x",
                "suggested_next_step": "y",
                "priority": 99,
                "lane": "urgent",
            }
        ],
    )
    assert result[0]["priority"] == 3


def test_map_results_tolerates_missing_why_it_matters_and_next_step() -> None:
    # JSON-mode providers (Ollama/Groq) have no schema enforcement — a
    # model that omits a required field shouldn't crash the whole batch
    # with a bare KeyError, same degrade-gracefully philosophy as every
    # other malformed-provider-output path in this module.
    raw = _raw_item()
    result = triage._map_results(
        [raw],
        [
            {
                "source": "gmail",
                "source_id": "m1",
                "priority": 1,
                "lane": "urgent",
            }
        ],
    )
    assert result == [
        {
            "lane": "urgent",
            "source": "gmail",
            "source_id": "m1",
            "title": "Test subject",
            "why_it_matters": "",
            "suggested_next_step": "",
            "priority": 1,
            "deep_link": "https://mail.google.com/x",
            "due_date": None,
        }
    ]


class _FakeCompletions:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def create(self, **kwargs):
        raise self._exc


class _FakeChat:
    def __init__(self, exc: Exception) -> None:
        self.completions = _FakeCompletions(exc)


class _FakeOpenAIClient:
    def __init__(self, exc: Exception, *args, **kwargs) -> None:
        self.chat = _FakeChat(exc)


def test_parse_items_json_handles_native_array() -> None:
    items = triage._parse_items_json('{"items": [{"source": "gmail"}]}', "Test")
    assert items == [{"source": "gmail"}]


def test_parse_items_json_unwraps_double_encoded_string() -> None:
    # Some local models emit items as a JSON-encoded string instead of a
    # native array despite the schema — this is the real shape observed
    # from llama3.1:8b-instruct-q4_K_M in practice.
    items = triage._parse_items_json('{"items": "[{\\"source\\": \\"gmail\\"}]"}', "Test")
    assert items == [{"source": "gmail"}]


def test_parse_items_json_returns_empty_on_garbage_string() -> None:
    items = triage._parse_items_json('{"items": "not valid json"}', "Test")
    assert items == []


def test_parse_items_json_raises_on_malformed_top_level_json() -> None:
    with pytest.raises(triage.TriageProviderError, match="malformed JSON"):
        triage._parse_items_json("this isn't JSON at all", "Test")


def test_run_ollama_wraps_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx2.Request("POST", "http://localhost:11434/v1/chat/completions")
    conn_error = openai.APIConnectionError(message="Connection refused", request=request)

    monkeypatch.setattr(
        triage.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClient(conn_error)
    )
    monkeypatch.setattr(triage, "TRIAGE_MODEL", "llama3.2:3b")

    with pytest.raises(triage.TriageProviderError, match="ollama serve"):
        triage._run_ollama([_raw_item()])


class _FakeUsage:
    def __init__(self) -> None:
        self.prompt_tokens = 100
        self.completion_tokens = 50


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeJsonResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeCompletionsJson:
    def __init__(self, content: str | None) -> None:
        self._content = content
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeJsonResponse(self._content)


class _FakeChatJson:
    def __init__(self, content: str | None) -> None:
        self.completions = _FakeCompletionsJson(content)


class _FakeOpenAIClientJson:
    def __init__(self, content: str | None, *args, **kwargs) -> None:
        self.chat = _FakeChatJson(content)


def test_run_ollama_happy_path_uses_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_content = json.dumps(
        {
            "items": [
                {
                    "source": "gmail",
                    "source_id": "m1",
                    "why_it_matters": "matters",
                    "suggested_next_step": "do it",
                    "priority": 1,
                    "lane": "urgent",
                }
            ]
        }
    )
    fake_client = _FakeOpenAIClientJson(fake_content)
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: fake_client)
    monkeypatch.setattr(triage, "TRIAGE_MODEL", "llama3.1:8b-instruct-q4_K_M")

    result = triage._run_ollama([_raw_item()])

    assert result[0]["title"] == "Test subject"
    assert result[0]["lane"] == "urgent"
    # No forced tool-calling for Ollama — json_object mode instead, per the
    # real-world reliability finding documented next to _run_ollama.
    sent_kwargs = fake_client.chat.completions.last_kwargs
    assert "tools" not in sent_kwargs
    assert "tool_choice" not in sent_kwargs
    assert sent_kwargs["response_format"] == {"type": "json_object"}


def test_run_ollama_raises_on_empty_content(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeOpenAIClientJson(None)
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: fake_client)

    with pytest.raises(triage.TriageProviderError, match="empty response"):
        triage._run_ollama([_raw_item()])


def test_run_groq_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "GROQ_API_KEY", None)
    with pytest.raises(triage.TriageProviderError, match="GROQ_API_KEY"):
        triage._run_groq([_raw_item()])


def test_run_groq_happy_path_uses_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_content = json.dumps(
        {
            "items": [
                {
                    "source": "gmail",
                    "source_id": "m1",
                    "why_it_matters": "matters",
                    "suggested_next_step": "do it",
                    "priority": 1,
                    "lane": "urgent",
                }
            ]
        }
    )
    fake_client = _FakeOpenAIClientJson(fake_content)
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_fake_key")
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: fake_client)
    monkeypatch.setattr(triage, "TRIAGE_MODEL", "llama-3.3-70b-versatile")

    result = triage._run_groq([_raw_item()])

    assert result[0]["title"] == "Test subject"
    assert result[0]["lane"] == "urgent"


def test_run_groq_wraps_authentication_error(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx2.Response(
        401, request=httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    auth_error = openai.AuthenticationError("Invalid API key", response=response, body=None)

    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_bad_key")
    monkeypatch.setattr(
        triage.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClient(auth_error)
    )

    with pytest.raises(triage.TriageProviderError, match="authentication error"):
        triage._run_groq([_raw_item()])


def test_run_groq_chat_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "GROQ_API_KEY", None)
    with pytest.raises(triage.TriageProviderError, match="GROQ_API_KEY"):
        triage.run_groq_chat([{"role": "user", "content": "hi"}])


def test_run_groq_chat_returns_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeOpenAIClientJson("The answer is 42.")
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_fake_key")
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: fake_client)

    result = triage.run_groq_chat([{"role": "user", "content": "What is the answer?"}])

    assert result == "The answer is 42."
    # Free-text chat, not triage's forced JSON mode.
    sent_kwargs = fake_client.chat.completions.last_kwargs
    assert "response_format" not in sent_kwargs


def test_run_groq_chat_raises_on_empty_content(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeOpenAIClientJson(None)
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_fake_key")
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: fake_client)

    with pytest.raises(triage.TriageProviderError, match="empty response"):
        triage.run_groq_chat([{"role": "user", "content": "hi"}])


def test_run_groq_chat_wraps_authentication_error(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx2.Response(
        401, request=httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    auth_error = openai.AuthenticationError("Invalid API key", response=response, body=None)

    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_bad_key")
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClient(auth_error))

    with pytest.raises(triage.TriageProviderError, match="authentication error"):
        triage.run_groq_chat([{"role": "user", "content": "hi"}])


def test_run_groq_chat_wraps_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression test: hit for real during Part 2 live verification — a
    # 429 from Groq was propagating as a raw openai.RateLimitError,
    # uncaught by TriageProviderError, which would 500 the whole
    # /history/report page instead of degrading just the reflection.
    response = httpx2.Response(
        429, request=httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    rate_limit_error = openai.RateLimitError("Rate limit reached", response=response, body=None)

    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_bad_key")
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClient(rate_limit_error))

    with pytest.raises(triage.TriageProviderError, match="rate limit"):
        triage.run_groq_chat([{"role": "user", "content": "hi"}])


def test_run_groq_chat_wraps_bad_request_error_from_a_malformed_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression test: hit for real live-testing Part 4's lane/project
    # tool-calling — Groq's own tool-call parser rejected the model's
    # output ("tool_use_failed") even though the model's reasoning was
    # correct, and openai.BadRequestError had no handler here, so it
    # propagated as a raw 500 instead of the router's existing clean
    # 502 degrade path (same as every other provider failure).
    response = httpx2.Response(
        400, request=httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    bad_request_error = openai.BadRequestError(
        "Failed to call a function. Please adjust your prompt.", response=response, body=None
    )

    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_bad_key")
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClient(bad_request_error))

    with pytest.raises(triage.TriageProviderError, match="Failed to call a function"):
        triage.run_groq_chat([{"role": "user", "content": "hi"}])


# --- primary/backup Groq key fallback -----------------------------------------


def _rate_limit_error() -> openai.RateLimitError:
    response = httpx2.Response(
        429, request=httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    return openai.RateLimitError("Rate limit reached", response=response, body=None)


def test_groq_fallback_retries_backup_key_on_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_backup_client = _FakeOpenAIClientJson("Backup worked.")
    fake_primary_client = _FakeOpenAIClient(_rate_limit_error())

    def _fake_openai(**kwargs):
        return fake_backup_client if kwargs.get("api_key") == "gsk_backup" else fake_primary_client

    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_primary")
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP", "gsk_backup")
    monkeypatch.setattr(triage.openai, "OpenAI", _fake_openai)

    result = triage.run_groq_chat([{"role": "user", "content": "hi"}])

    assert result == "Backup worked."


def test_groq_fallback_not_attempted_without_backup_key_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_primary")
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP", None)
    monkeypatch.setattr(
        triage.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClient(_rate_limit_error())
    )

    with pytest.raises(triage.TriageProviderError, match="rate limit"):
        triage.run_groq_chat([{"role": "user", "content": "hi"}])


def test_groq_fallback_retries_backup_key_on_authentication_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not scoped to rate-limit errors — an auth error (e.g. the primary
    # key was revoked or rotated) is exactly the kind of failure a
    # second account's key plausibly fixes too. Confirmed live: the
    # primary key here went from rate-limited to outright invalid
    # between one check and the next, and only the backup key still
    # authenticated.
    response = httpx2.Response(
        401, request=httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    auth_error = openai.AuthenticationError("Invalid API key", response=response, body=None)
    fake_backup_client = _FakeOpenAIClientJson("Backup worked.")
    fake_primary_client = _FakeOpenAIClient(auth_error)

    def _fake_openai(**kwargs):
        return fake_backup_client if kwargs.get("api_key") == "gsk_backup" else fake_primary_client

    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_primary")
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP", "gsk_backup")
    monkeypatch.setattr(triage.openai, "OpenAI", _fake_openai)

    result = triage.run_groq_chat([{"role": "user", "content": "hi"}])

    assert result == "Backup worked."


def test_groq_fallback_raises_if_backup_also_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx2.Response(
        401, request=httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    auth_error = openai.AuthenticationError("Invalid API key", response=response, body=None)

    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_primary")
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP", "gsk_backup")
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClient(auth_error))

    with pytest.raises(triage.TriageProviderError, match="authentication error"):
        triage.run_groq_chat([{"role": "user", "content": "hi"}])


def test_groq_fallback_raises_if_backup_also_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_primary")
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP", "gsk_backup")
    monkeypatch.setattr(
        triage.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClient(_rate_limit_error())
    )

    with pytest.raises(triage.TriageProviderError, match="rate limit"):
        triage.run_groq_chat([{"role": "user", "content": "hi"}])


def test_groq_fallback_primary_success_never_touches_backup_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeOpenAIClientJson("Primary worked.")
    calls: list[str | None] = []

    def _fake_openai(**kwargs):
        calls.append(kwargs.get("api_key"))
        return fake_client

    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_primary")
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP", "gsk_backup")
    monkeypatch.setattr(triage.openai, "OpenAI", _fake_openai)

    result = triage.run_groq_chat([{"role": "user", "content": "hi"}])

    assert result == "Primary worked."
    assert calls == ["gsk_primary"]


# --- three-key rotation, cooldown parsing, and cross-call skip behavior -------


def test_groq_rotation_falls_through_three_keys_to_the_one_that_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_success = _FakeOpenAIClientJson("Third key worked.")
    fake_failure_1 = _FakeOpenAIClient(_rate_limit_error())
    fake_failure_2 = _FakeOpenAIClient(_rate_limit_error())

    def _fake_openai(**kwargs):
        return {
            "gsk_primary": fake_failure_1,
            "gsk_backup": fake_failure_2,
            "gsk_backup_2": fake_success,
        }[kwargs["api_key"]]

    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_primary")
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP", "gsk_backup")
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP_2", "gsk_backup_2")
    monkeypatch.setattr(triage.openai, "OpenAI", _fake_openai)

    result = triage.run_groq_chat([{"role": "user", "content": "hi"}])

    assert result == "Third key worked."


def test_groq_rotation_parses_retry_after_into_a_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real Groq error text observed live: "Please try again in 24m15.84s."
    response = httpx2.Response(
        429, request=httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    rate_limit_error = openai.RateLimitError(
        "Rate limit reached. Please try again in 1m5.5s.", response=response, body=None
    )
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_primary")
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP", "gsk_backup")
    monkeypatch.setattr(
        triage.openai,
        "OpenAI",
        lambda **kwargs: fake_backup if kwargs["api_key"] == "gsk_backup" else fake_primary,
    )
    fake_primary = _FakeOpenAIClient(rate_limit_error)
    fake_backup = _FakeOpenAIClientJson("Backup worked.")

    before = triage.datetime.now(triage.TZ)
    triage.run_groq_chat([{"role": "user", "content": "hi"}])
    after = triage.datetime.now(triage.TZ)

    cooldown_until = triage._key_cooldowns["gsk_primary"]
    # ~65.5 seconds out, not the generic 300s default — proves it parsed
    # "1m5.5s" rather than falling back to _DEFAULT_KEY_COOLDOWN_SECONDS.
    assert (before.timestamp() + 60) < cooldown_until.timestamp() < (after.timestamp() + 70)


def test_groq_rotation_skips_a_key_still_in_cooldown_on_the_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_backup = _FakeOpenAIClientJson("Backup worked.")
    calls: list[str] = []

    def _fake_openai(**kwargs):
        calls.append(kwargs["api_key"])
        if kwargs["api_key"] == "gsk_primary":
            return _FakeOpenAIClient(_rate_limit_error())
        return fake_backup

    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_primary")
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP", "gsk_backup")
    monkeypatch.setattr(triage.openai, "OpenAI", _fake_openai)

    # First call: primary fails and gets cooled down, backup succeeds.
    triage.run_groq_chat([{"role": "user", "content": "hi"}])
    # Second call, same process: primary is still within its cooldown
    # window, so it should be skipped entirely, going straight to backup.
    calls.clear()
    triage.run_groq_chat([{"role": "user", "content": "hi"}])

    assert calls == ["gsk_backup"]  # primary never retried this soon


def test_groq_rotation_uses_default_cooldown_for_non_rate_limit_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx2.Response(
        401, request=httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    auth_error = openai.AuthenticationError("Invalid API key", response=response, body=None)
    fake_backup = _FakeOpenAIClientJson("Backup worked.")

    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_primary")
    monkeypatch.setattr(triage, "GROQ_API_KEY_BACKUP", "gsk_backup")
    monkeypatch.setattr(
        triage.openai,
        "OpenAI",
        lambda **kwargs: fake_backup if kwargs["api_key"] == "gsk_backup" else _FakeOpenAIClient(auth_error),
    )

    before = triage.datetime.now(triage.TZ)
    triage.run_groq_chat([{"role": "user", "content": "hi"}])

    cooldown_until = triage._key_cooldowns["gsk_primary"]
    expected = before.timestamp() + triage._DEFAULT_KEY_COOLDOWN_SECONDS
    assert abs(cooldown_until.timestamp() - expected) < 5  # within a few seconds' slack


def test_parse_retry_after_seconds_handles_minutes_and_seconds() -> None:
    assert triage._parse_retry_after_seconds("Please try again in 24m15.84s.") == 24 * 60 + 15.84


def test_parse_retry_after_seconds_handles_seconds_only() -> None:
    assert triage._parse_retry_after_seconds("Please try again in 45s.") == 45.0


def test_parse_retry_after_seconds_returns_none_when_absent() -> None:
    assert triage._parse_retry_after_seconds("Some unrelated error message.") is None


class _FakeFunctionCall:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, name: str, arguments: str) -> None:
        self.function = _FakeFunctionCall(name, arguments)


class _FakeMessageWithTools:
    def __init__(self, content: str | None, tool_calls: list | None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoiceWithTools:
    def __init__(self, content: str | None, tool_calls: list | None) -> None:
        self.message = _FakeMessageWithTools(content, tool_calls)


class _FakeToolResponse:
    def __init__(self, content: str | None, tool_calls: list | None) -> None:
        self.choices = [_FakeChoiceWithTools(content, tool_calls)]
        self.usage = _FakeUsage()


class _FakeCompletionsWithTools:
    def __init__(self, content: str | None, tool_calls: list | None) -> None:
        self._content = content
        self._tool_calls = tool_calls
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeToolResponse(self._content, self._tool_calls)


class _FakeChatWithTools:
    def __init__(self, content: str | None, tool_calls: list | None) -> None:
        self.completions = _FakeCompletionsWithTools(content, tool_calls)


class _FakeOpenAIClientWithTools:
    def __init__(self, content: str | None, tool_calls: list | None = None) -> None:
        self.chat = _FakeChatWithTools(content, tool_calls)


def test_run_groq_chat_with_tools_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "GROQ_API_KEY", None)
    with pytest.raises(triage.TriageProviderError, match="GROQ_API_KEY"):
        triage.run_groq_chat_with_tools([{"role": "user", "content": "hi"}], tools=[])


def test_run_groq_chat_with_tools_plain_text_when_no_tool_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeOpenAIClientWithTools("Here's the answer.", tool_calls=None)
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_fake")
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: fake_client)

    result = triage.run_groq_chat_with_tools(
        [{"role": "user", "content": "hi"}], tools=[{"fake": "schema"}]
    )

    assert result == {"content": "Here's the answer.", "tool_calls": []}
    # tool_choice="auto" — never forced — so a plain question isn't coerced.
    sent_kwargs = fake_client.chat.completions.last_kwargs
    assert sent_kwargs["tool_choice"] == "auto"
    assert sent_kwargs["tools"] == [{"fake": "schema"}]


def test_run_groq_chat_with_tools_parses_a_real_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = _FakeToolCall("create_task", json.dumps({"title": "Renew passport"}))
    fake_client = _FakeOpenAIClientWithTools(None, tool_calls=[tool_call])
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_fake")
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: fake_client)

    result = triage.run_groq_chat_with_tools(
        [{"role": "user", "content": "create a task"}], tools=[]
    )

    assert result["content"] is None
    assert result["tool_calls"] == [{"name": "create_task", "arguments": {"title": "Renew passport"}}]


def test_run_groq_chat_with_tools_handles_malformed_json_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = _FakeToolCall("create_task", "{not valid json")
    fake_client = _FakeOpenAIClientWithTools(None, tool_calls=[tool_call])
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_fake")
    monkeypatch.setattr(triage.openai, "OpenAI", lambda **kwargs: fake_client)

    result = triage.run_groq_chat_with_tools(
        [{"role": "user", "content": "create a task"}], tools=[]
    )

    assert result["tool_calls"] == [{"name": "create_task", "arguments": None}]


# --- _run_anthropic ----------------------------------------------------------


class _FakeAnthropicUsage:
    def __init__(self) -> None:
        self.input_tokens = 10
        self.output_tokens = 5


class _FakeAnthropicToolUseBlock:
    type = "tool_use"

    def __init__(self, items: list[dict]) -> None:
        self.input = {"items": items}


class _FakeAnthropicResponse:
    def __init__(self, items: list[dict]) -> None:
        self.content = [_FakeAnthropicToolUseBlock(items)]
        self.usage = _FakeAnthropicUsage()


class _FakeAnthropicMessages:
    def __init__(self, result) -> None:
        self._result = result

    def create(self, **kwargs):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeAnthropicClient:
    def __init__(self, result, *args, **kwargs) -> None:
        self.messages = _FakeAnthropicMessages(result)


def test_run_anthropic_parses_a_real_shaped_tool_use_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_response = _FakeAnthropicResponse(
        [{"source": "gmail", "source_id": "m1", "lane": "urgent", "priority": 1,
          "why_it_matters": "x", "suggested_next_step": "y"}]
    )
    monkeypatch.setattr(triage, "ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setattr(
        triage.anthropic, "Anthropic", lambda **kwargs: _FakeAnthropicClient(fake_response)
    )

    result = triage._run_anthropic([_raw_item()])

    assert result[0]["source_id"] == "m1"
    assert result[0]["lane"] == "urgent"


def test_run_anthropic_wraps_authentication_error(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx2.Response(
        401, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    auth_error = anthropic.AuthenticationError("Invalid API key", response=response, body=None)

    monkeypatch.setattr(triage, "ANTHROPIC_API_KEY", "sk-ant-bad")
    monkeypatch.setattr(
        triage.anthropic, "Anthropic", lambda **kwargs: _FakeAnthropicClient(auth_error)
    )

    with pytest.raises(triage.TriageProviderError, match="authentication error"):
        triage._run_anthropic([_raw_item()])


def test_run_anthropic_wraps_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    conn_error = anthropic.APIConnectionError(message="Connection error.", request=request)

    monkeypatch.setattr(triage, "ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setattr(
        triage.anthropic, "Anthropic", lambda **kwargs: _FakeAnthropicClient(conn_error)
    )

    with pytest.raises(triage.TriageProviderError, match="Anthropic request failed"):
        triage._run_anthropic([_raw_item()])


def test_run_anthropic_wraps_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx2.Response(
        429, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    rate_limit_error = anthropic.RateLimitError("Rate limit reached", response=response, body=None)

    monkeypatch.setattr(triage, "ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setattr(
        triage.anthropic, "Anthropic", lambda **kwargs: _FakeAnthropicClient(rate_limit_error)
    )

    with pytest.raises(triage.TriageProviderError, match="rate limit reached"):
        triage._run_anthropic([_raw_item()])


def test_run_anthropic_wraps_bad_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx2.Response(
        400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    bad_request_error = anthropic.BadRequestError("Malformed request", response=response, body=None)

    monkeypatch.setattr(triage, "ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setattr(
        triage.anthropic, "Anthropic", lambda **kwargs: _FakeAnthropicClient(bad_request_error)
    )

    with pytest.raises(triage.TriageProviderError, match="Anthropic request failed"):
        triage._run_anthropic([_raw_item()])
