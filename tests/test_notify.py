import httpx
import pytest

from command_center import notify


def test_is_configured_false_when_topic_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "NTFY_TOPIC", "")
    assert notify.is_configured() is False


def test_is_configured_true_when_topic_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "NTFY_TOPIC", "my-topic")
    assert notify.is_configured() is True


def test_send_returns_false_and_does_not_call_httpx_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify, "NTFY_TOPIC", "")
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append((a, k)))

    result = notify.send("Title", "Message")

    assert result is False
    assert calls == []


def test_send_posts_to_the_configured_server_and_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "NTFY_TOPIC", "my-topic")
    monkeypatch.setattr(notify, "NTFY_SERVER", "https://ntfy.sh")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, content, headers, timeout):
        calls.append({"url": url, "content": content, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    result = notify.send("Daily Command Center", "3 items overdue", priority="high", tags=["warning"])

    assert result is True
    assert calls[0]["url"] == "https://ntfy.sh/my-topic"
    assert calls[0]["content"] == b"3 items overdue"
    assert calls[0]["headers"]["Title"] == "Daily Command Center"
    assert calls[0]["headers"]["Priority"] == "high"
    assert calls[0]["headers"]["Tags"] == "warning"


def test_send_strips_trailing_slash_from_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "NTFY_TOPIC", "my-topic")
    monkeypatch.setattr(notify, "NTFY_SERVER", "https://ntfy.sh/")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        httpx, "post", lambda url, **k: calls.append(url) or FakeResponse()
    )

    notify.send("Title", "Message")

    assert calls[0] == "https://ntfy.sh/my-topic"


def test_send_returns_false_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "NTFY_TOPIC", "my-topic")

    def fake_post(*a, **k):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = notify.send("Title", "Message")

    assert result is False


def test_send_returns_false_on_non_2xx_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "NTFY_TOPIC", "my-topic")

    class FakeResponse:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse())

    result = notify.send("Title", "Message")

    assert result is False


def test_send_without_tags_omits_the_tags_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "NTFY_TOPIC", "my-topic")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, content, headers, timeout):
        calls.append(headers)
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    notify.send("Title", "Message")

    assert "Tags" not in calls[0]
