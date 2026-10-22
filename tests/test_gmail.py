import base64

from command_center.sources.gmail import GmailSource, _decode_part, _find_body, _header


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def test_decode_part_handles_missing_padding() -> None:
    # urlsafe_b64encode's own output for "hi" has no padding stripped, so
    # exercise the padding-restore path with a string whose length isn't
    # a multiple of 4 once encoded without the trailing '='.
    encoded = base64.urlsafe_b64encode(b"hello world").decode("ascii").rstrip("=")
    assert _decode_part(encoded) == "hello world"


def test_header_is_case_insensitive_and_defaults_to_empty() -> None:
    headers = [{"name": "Subject", "value": "Hi there"}, {"name": "From", "value": "a@b.com"}]
    assert _header(headers, "subject") == "Hi there"
    assert _header(headers, "FROM") == "a@b.com"
    assert _header(headers, "Missing") == ""


def test_find_body_prefers_plain_text_over_html() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
            {"mimeType": "text/plain", "body": {"data": _b64("plain text")}},
        ],
    }
    text, is_html = _find_body(payload)
    assert text == "plain text"
    assert is_html is False


def test_find_body_falls_back_to_html_when_no_plain_part() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [{"mimeType": "text/html", "body": {"data": _b64("<p>only html</p>")}}],
    }
    text, is_html = _find_body(payload)
    assert text == "<p>only html</p>"
    assert is_html is True


def test_find_body_walks_nested_parts() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [{"mimeType": "text/plain", "body": {"data": _b64("nested plain")}}],
            }
        ],
    }
    text, is_html = _find_body(payload)
    assert text == "nested plain"
    assert is_html is False


def test_find_body_returns_empty_when_no_recognized_part() -> None:
    payload = {"mimeType": "application/octet-stream", "parts": []}
    assert _find_body(payload) == ("", False)


class _FakeExecutable:
    def __init__(self, result: dict) -> None:
        self._result = result

    def execute(self) -> dict:
        return self._result


class _FakeMessages:
    def __init__(self, list_result: dict, get_results: dict[str, dict]) -> None:
        self._list_result = list_result
        self._get_results = get_results

    def list(self, userId: str, q: str, maxResults: int) -> _FakeExecutable:
        return _FakeExecutable(self._list_result)

    def get(self, userId: str, id: str, format: str) -> _FakeExecutable:
        return _FakeExecutable(self._get_results[id])


class _FakeUsers:
    def __init__(self, list_result: dict, get_results: dict[str, dict]) -> None:
        self._messages = _FakeMessages(list_result, get_results)

    def messages(self) -> _FakeMessages:
        return self._messages


class _FakeGmailService:
    def __init__(self, list_result: dict, get_results: dict[str, dict]) -> None:
        self._users = _FakeUsers(list_result, get_results)

    def users(self) -> _FakeUsers:
        return self._users


def test_fetch_builds_raw_items_from_messages() -> None:
    source = GmailSource.__new__(GmailSource)
    source._service = _FakeGmailService(
        list_result={"messages": [{"id": "msg1"}]},
        get_results={
            "msg1": {
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Hello"},
                        {"name": "From", "value": "someone@example.com"},
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": _b64("Body text")},
                }
            }
        },
    )

    items = source.fetch()

    assert len(items) == 1
    item = items[0]
    assert item.source == "gmail"
    assert item.source_id == "msg1"
    assert item.title == "Hello"
    assert item.body == "Body text"
    assert item.metadata["from"] == "someone@example.com"
    assert item.metadata["deep_link"] == "https://mail.google.com/mail/u/0/#inbox/msg1"


def test_fetch_defaults_missing_subject() -> None:
    source = GmailSource.__new__(GmailSource)
    source._service = _FakeGmailService(
        list_result={"messages": [{"id": "msg1"}]},
        get_results={"msg1": {"payload": {"headers": [], "mimeType": "text/plain", "body": {}}}},
    )

    items = source.fetch()

    assert items[0].title == "(no subject)"


def test_fetch_returns_empty_list_when_no_messages() -> None:
    source = GmailSource.__new__(GmailSource)
    source._service = _FakeGmailService(list_result={}, get_results={})

    assert source.fetch() == []
