"""Pure text-cleaning helpers for email bodies. No dependencies beyond stdlib —
Gmail hands back a text/plain MIME part for the vast majority of real mail,
so full HTML parsing isn't needed; this is a deliberately simple tag-stripper
for the HTML-only fallback case.
"""

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_QUOTE_MARKERS = (
    re.compile(r"^On .{0,120}wrote:\s*$", re.MULTILINE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^>", re.MULTILINE),
)
_SIGNATURE_MARKER = re.compile(r"^--\s*$", re.MULTILINE)


def strip_html(raw_html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw_html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    return html.unescape(text)


def strip_quoted_reply(text: str) -> str:
    cut_at = len(text)
    for pattern in _QUOTE_MARKERS:
        match = pattern.search(text)
        if match:
            cut_at = min(cut_at, match.start())
    return text[:cut_at]


def strip_signature(text: str) -> str:
    match = _SIGNATURE_MARKER.search(text)
    return text[: match.start()] if match else text


def normalize_whitespace(text: str) -> str:
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def clean_email_body(raw: str, is_html: bool, max_chars: int = 800) -> str:
    text = strip_html(raw) if is_html else raw
    text = strip_quoted_reply(text)
    text = strip_signature(text)
    text = normalize_whitespace(text)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text
