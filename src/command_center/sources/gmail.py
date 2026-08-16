"""Gmail ingestion: unread + important threads from the last 24h."""

import base64

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from command_center.sources import RawItem
from command_center.sources.text_clean import clean_email_body

QUERY = "(is:unread OR is:important) newer_than:1d"
MAX_RESULTS = 25


def _decode_part(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _find_body(payload: dict) -> tuple[str, bool]:
    """Walks MIME parts, preferring text/plain. Returns (text, is_html)."""
    plain: str | None = None
    html_body: str | None = None
    stack = [payload]
    while stack:
        part = stack.pop()
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mime == "text/plain" and data and plain is None:
            plain = _decode_part(data)
        elif mime == "text/html" and data and html_body is None:
            html_body = _decode_part(data)
        stack.extend(part.get("parts", []))
    if plain is not None:
        return plain, False
    if html_body is not None:
        return html_body, True
    return "", False


class GmailSource:
    def __init__(self, credentials: Credentials) -> None:
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def fetch(self) -> list[RawItem]:
        resp = (
            self._service.users()
            .messages()
            .list(userId="me", q=QUERY, maxResults=MAX_RESULTS)
            .execute()
        )
        refs = resp.get("messages", [])

        items = []
        for ref in refs:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=ref["id"], format="full")
                .execute()
            )
            payload = msg.get("payload", {})
            headers = payload.get("headers", [])
            subject = _header(headers, "Subject") or "(no subject)"
            sender = _header(headers, "From")
            raw_text, is_html = _find_body(payload)
            body = clean_email_body(raw_text, is_html)

            items.append(
                RawItem(
                    source="gmail",
                    source_id=ref["id"],
                    title=subject,
                    body=body,
                    metadata={
                        "from": sender,
                        "deep_link": f"https://mail.google.com/mail/u/0/#inbox/{ref['id']}",
                    },
                )
            )
        return items
