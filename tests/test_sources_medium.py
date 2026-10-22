import json

import httpx
import pytest

from command_center.sources import medium


class _FakeResponse:
    def __init__(self, json_data: object, status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://medium2.p.rapidapi.com")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=httpx.Response(self.status_code, request=request)
            )

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


def test_fetch_and_rank_requires_credentials_configured(monkeypatch) -> None:
    monkeypatch.setattr(medium, "MEDIUM_API_KEY", None)
    monkeypatch.setattr(medium, "MEDIUM_USERNAME", "zerdanabdessamad")
    with pytest.raises(medium.TriageProviderError, match="MEDIUM_API_KEY"):
        medium.fetch_and_rank()
