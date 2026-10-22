import json
from datetime import datetime, timedelta

import httpx2
import openai
import pytest

from command_center import db, triage


@pytest.fixture(autouse=True)
def _settings_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The triage prompt reads the summary-language setting from the DB,
    as it does in the real app — so every test here gets a fresh one."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


@pytest.fixture(autouse=True)
def _fresh_groq_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Groq pause (_groq_paused_until) is process-lifetime state — reset
    it so one test's rate limit can't leak into the next. And no test may
    probe a real local Ollama: the rate-limit fallback asks for one, so it's
    "not installed" unless a test says otherwise."""
    monkeypatch.setattr(triage, "_groq_paused_until", None)
    monkeypatch.setattr(triage, "_local_fallback_model", lambda: None)


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


def test_parse_due_date_valid_rfc3339() -> None:
    assert triage._parse_due_date("2026-08-20T00:00:00.000Z") == "2026-08-20"


def test_parse_due_date_missing() -> None:
    assert triage._parse_due_date(None) is None
    assert triage._parse_due_date("") is None


def test_parse_due_date_malformed_never_raises() -> None:
    assert triage._parse_due_date("not-a-date") is None


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

    with pytest.raises(triage.TriageProviderError, match="rate-limited"):
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


# --- one Groq key: back off on rate limits, fall back to local Ollama -----------


def _rate_limit_error(message: str = "Rate limit reached") -> openai.RateLimitError:
    response = httpx2.Response(
        429, request=httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    return openai.RateLimitError(message, response=response, body=None)


class _ScriptedClients:
    """openai.OpenAI stand-in: Groq answers with the scripted results in
    order (an exception is raised, anything else returned as JSON
    content); Ollama always answers `local_content`. Records every
    client's api_key and base_url."""

    def __init__(self, groq_results: list, local_content: str = "{}") -> None:
        self.groq_results = list(groq_results)
        self.local_content = local_content
        self.made: list[tuple[str, str]] = []
        self.groq_calls = 0

    def __call__(self, base_url: str = "", api_key: str = "", **kwargs):
        self.made.append((base_url, api_key))
        if "11434" in base_url:
            return _FakeOpenAIClientJson(self.local_content)
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.groq_calls += 1
                result = outer.groq_results.pop(0)
                if isinstance(result, Exception):
                    raise result
                return _FakeJsonResponse(result)

        client = type("C", (), {})()
        client.chat = type("Chat", (), {"completions": _Completions()})()
        return client


def test_only_the_one_configured_key_is_ever_used(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import config

    clients = _ScriptedClients([_rate_limit_error()])
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_only")
    monkeypatch.setattr(triage.openai, "OpenAI", clients)

    with pytest.raises(triage.ProviderRateLimited):
        triage.run_groq_chat([{"role": "user", "content": "hi"}])

    assert {key for _, key in clients.made} == {"gsk_only"}
    assert not hasattr(config, "GROQ_API_KEY_BACKUP") and not hasattr(triage, "_groq_keys")


def test_a_short_rate_limit_is_waited_out_once(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    clients = _ScriptedClients([_rate_limit_error("Rate limit reached. Please try again in 2.5s."), "ok after waiting"])
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_only")
    monkeypatch.setattr(triage.openai, "OpenAI", clients)
    monkeypatch.setattr(triage.time, "sleep", slept.append)

    assert triage.run_groq_chat([{"role": "user", "content": "hi"}]) == "ok after waiting"
    assert slept == [2.5] and clients.groq_calls == 2


def test_a_long_rate_limit_pauses_groq_instead_of_hammering_it(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = _ScriptedClients([_rate_limit_error("Rate limit reached. Please try again in 4m10s.")])
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_only")
    monkeypatch.setattr(triage.openai, "OpenAI", clients)
    monkeypatch.setattr(triage.time, "sleep", lambda s: pytest.fail("must not sleep for minutes"))

    with pytest.raises(triage.ProviderRateLimited, match="try again in 250s"):
        triage.run_groq_chat([{"role": "user", "content": "hi"}])
    with pytest.raises(triage.ProviderRateLimited, match="rate-limited"):
        triage.run_groq_chat([{"role": "user", "content": "again"}])

    assert clients.groq_calls == 1  # the second call never reached Groq


def test_groq_is_tried_again_once_the_pause_is_over(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = _ScriptedClients(["back"])
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_only")
    monkeypatch.setattr(triage.openai, "OpenAI", clients)
    monkeypatch.setattr(triage, "_groq_paused_until", datetime.now(triage.TZ) - timedelta(seconds=1))

    assert triage.run_groq_chat([{"role": "user", "content": "hi"}]) == "back"


def test_tool_calling_never_falls_back_to_the_local_model(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = _ScriptedClients([_rate_limit_error("try again in 3m0s")])
    monkeypatch.setattr(triage, "GROQ_API_KEY", "gsk_only")
    monkeypatch.setattr(triage.openai, "OpenAI", clients)
    monkeypatch.setattr(triage, "_local_fallback_model", lambda: "llama3.2:3b")

    with pytest.raises(triage.ProviderRateLimited):
        triage.run_groq_chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert not any("11434" in base for base, _ in clients.made)


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
