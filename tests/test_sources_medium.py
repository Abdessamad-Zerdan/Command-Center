import json

import pytest

from command_center.sources import medium


class _FakeResponse:
    def __init__(self, json_data: object, status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self._json_data


class _FakeHttpxClient:
    """Matches request URLs by substring against a fixed response map."""

    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self._responses = responses

    def get(self, url: str, headers: dict | None = None) -> _FakeResponse:
        for key, response in self._responses.items():
            if key in url:
                return response
        raise AssertionError(f"Unexpected URL requested: {url}")

    def __enter__(self) -> "_FakeHttpxClient":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _ExplodingClient:
    def get(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("should not call the API when the cache is already valid")


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 10


class _FakeCompletionsJson:
    def __init__(self, content: str | None) -> None:
        self._content = content

    def create(self, **kwargs: object):
        return type("R", (), {"choices": [_FakeChoice(self._content)], "usage": _FakeUsage()})()


class _FakeOpenAIClientJson:
    def __init__(self, content: str | None) -> None:
        self.chat = type("C", (), {"completions": _FakeCompletionsJson(content)})()


def test_resolve_user_id_calls_api_and_caches_on_first_use(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "medium_user_id.json"
    monkeypatch.setattr(medium, "MEDIUM_USER_ID_CACHE", cache_path)
    monkeypatch.setattr(medium, "MEDIUM_USERNAME", "zerdanabdessamad")

    fake_client = _FakeHttpxClient(
        {"/user/id_for/zerdanabdessamad": _FakeResponse({"id": "user-123"})}
    )

    user_id = medium._resolve_user_id(fake_client)

    assert user_id == "user-123"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached == {"username": "zerdanabdessamad", "user_id": "user-123"}


def test_resolve_user_id_reuses_cache_without_calling_api(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "medium_user_id.json"
    cache_path.write_text(
        json.dumps({"username": "zerdanabdessamad", "user_id": "cached-id"}), encoding="utf-8"
    )
    monkeypatch.setattr(medium, "MEDIUM_USER_ID_CACHE", cache_path)
    monkeypatch.setattr(medium, "MEDIUM_USERNAME", "zerdanabdessamad")

    user_id = medium._resolve_user_id(_ExplodingClient())

    assert user_id == "cached-id"


def test_resolve_user_id_reresolves_when_username_changes(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "medium_user_id.json"
    cache_path.write_text(
        json.dumps({"username": "old-user", "user_id": "old-id"}), encoding="utf-8"
    )
    monkeypatch.setattr(medium, "MEDIUM_USER_ID_CACHE", cache_path)
    monkeypatch.setattr(medium, "MEDIUM_USERNAME", "new-user")

    fake_client = _FakeHttpxClient({"/user/id_for/new-user": _FakeResponse({"id": "new-id"})})

    user_id = medium._resolve_user_id(fake_client)

    assert user_id == "new-id"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["username"] == "new-user"


def test_rank_articles_selects_and_orders_top_picks(monkeypatch) -> None:
    monkeypatch.setattr(medium, "GROQ_API_KEY", "fake-key")
    fake_content = json.dumps(
        {"picks": [{"id": "a2", "why": "directly relevant"}, {"id": "a1", "why": "tangential"}]}
    )
    monkeypatch.setattr(medium.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClientJson(fake_content))

    articles = [
        {"id": "a1", "title": "Intro to Python", "subtitle": "basics", "url": "https://medium.com/a1"},
        {
            "id": "a2",
            "title": "RAG pipelines in production",
            "subtitle": "legal-tech case study",
            "url": "https://medium.com/a2",
        },
    ]

    ranked = medium._rank_articles(articles, interests=["ai", "rag"])

    assert [a["id"] for a in ranked] == ["a2", "a1"]
    assert ranked[0]["why"] == "directly relevant"


def test_rank_articles_requires_groq_key(monkeypatch) -> None:
    monkeypatch.setattr(medium, "GROQ_API_KEY", None)
    with pytest.raises(medium.TriageProviderError, match="GROQ_API_KEY"):
        medium._rank_articles([{"id": "a1", "title": "x", "subtitle": "", "url": ""}], [])


def test_rank_articles_returns_empty_for_no_articles(monkeypatch) -> None:
    monkeypatch.setattr(medium, "GROQ_API_KEY", "fake-key")
    assert medium._rank_articles([], []) == []


def test_rank_articles_skips_ids_not_in_the_source_list(monkeypatch) -> None:
    monkeypatch.setattr(medium, "GROQ_API_KEY", "fake-key")
    fake_content = json.dumps({"picks": [{"id": "does-not-exist", "why": "x"}]})
    monkeypatch.setattr(medium.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClientJson(fake_content))

    ranked = medium._rank_articles([{"id": "a1", "title": "x", "subtitle": "", "url": ""}], [])

    assert ranked == []


def test_fetch_and_rank_requires_credentials_configured(monkeypatch) -> None:
    monkeypatch.setattr(medium, "MEDIUM_API_KEY", None)
    monkeypatch.setattr(medium, "MEDIUM_USERNAME", "zerdanabdessamad")
    with pytest.raises(medium.TriageProviderError, match="MEDIUM_API_KEY"):
        medium.fetch_and_rank()


def test_fetch_and_rank_end_to_end_returns_reading_lane_items(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(medium, "MEDIUM_API_KEY", "fake-key")
    monkeypatch.setattr(medium, "MEDIUM_USERNAME", "zerdanabdessamad")
    monkeypatch.setattr(medium, "MEDIUM_USER_ID_CACHE", tmp_path / "cache.json")
    monkeypatch.setattr(medium, "GROQ_API_KEY", "fake-key")

    responses = {
        "/user/id_for/zerdanabdessamad": _FakeResponse({"id": "user-1"}),
        "/user/user-1/interests": _FakeResponse(["ai", "legal-tech"]),
        "/user/user-1/following": _FakeResponse({"following": ["other-user"]}),
        "/user/user-1/articles": _FakeResponse({"associated_articles": ["a1", "a2"]}),
        "/article/a1": _FakeResponse(
            {"title": "Intro to Python", "subtitle": "basics", "url": "https://medium.com/a1"}
        ),
        "/article/a2": _FakeResponse(
            {
                "title": "RAG in legal-tech",
                "subtitle": "case study",
                "url": "https://medium.com/a2",
            }
        ),
    }
    fake_client = _FakeHttpxClient(responses)
    monkeypatch.setattr(medium.httpx, "Client", lambda timeout=None: fake_client)

    fake_rank_content = json.dumps({"picks": [{"id": "a2", "why": "directly relevant"}]})
    monkeypatch.setattr(
        medium.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClientJson(fake_rank_content)
    )

    items = medium.fetch_and_rank()

    assert len(items) == 1
    assert items[0]["lane"] == "reading"
    assert items[0]["source"] == "medium"
    assert items[0]["source_id"] == "a2"
    assert items[0]["title"] == "RAG in legal-tech"
    assert items[0]["why_it_matters"] == "directly relevant"
    assert items[0]["deep_link"] == "https://medium.com/a2"


def test_fetch_and_rank_swallows_interests_failure_and_continues(tmp_path, monkeypatch) -> None:
    """A single flaky endpoint (interests) must not sink the whole feed —
    articles/following are independently try/excepted."""
    monkeypatch.setattr(medium, "MEDIUM_API_KEY", "fake-key")
    monkeypatch.setattr(medium, "MEDIUM_USERNAME", "zerdanabdessamad")
    monkeypatch.setattr(medium, "MEDIUM_USER_ID_CACHE", tmp_path / "cache.json")
    monkeypatch.setattr(medium, "GROQ_API_KEY", "fake-key")

    responses = {
        "/user/id_for/zerdanabdessamad": _FakeResponse({"id": "user-1"}),
        # interests deliberately omitted -> _FakeHttpxClient.get raises AssertionError,
        # which _fetch_interests's broad except must catch and turn into [].
        "/user/user-1/following": _FakeResponse({"following": []}),
        "/user/user-1/articles": _FakeResponse({"associated_articles": ["a1"]}),
        "/article/a1": _FakeResponse(
            {"title": "Only article", "subtitle": "", "url": "https://medium.com/a1"}
        ),
    }
    fake_client = _FakeHttpxClient(responses)
    monkeypatch.setattr(medium.httpx, "Client", lambda timeout=None: fake_client)

    fake_rank_content = json.dumps({"picks": [{"id": "a1", "why": "only option"}]})
    monkeypatch.setattr(
        medium.openai, "OpenAI", lambda **kwargs: _FakeOpenAIClientJson(fake_rank_content)
    )

    items = medium.fetch_and_rank()

    assert len(items) == 1
    assert items[0]["source_id"] == "a1"
