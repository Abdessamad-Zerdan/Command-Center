from command_center.sources.text_clean import (
    clean_email_body,
    strip_html,
    strip_quoted_reply,
    strip_signature,
)


def test_strip_html_removes_tags_and_unescapes_entities() -> None:
    html_body = "<p>Hello &amp; welcome</p><br><div>Second line</div>"
    result = strip_html(html_body)
    assert "<p>" not in result
    assert "Hello & welcome" in result
    assert "Second line" in result


def test_strip_quoted_reply_cuts_at_on_wrote() -> None:
    text = "Sure, sounds good.\n\nOn Mon, Aug 11, 2026 at 9:00 AM John wrote:\n> original message"
    result = strip_quoted_reply(text)
    assert "Sure, sounds good." in result
    assert "original message" not in result


def test_strip_quoted_reply_cuts_at_original_message_marker() -> None:
    text = "Thanks!\n-----Original Message-----\nFrom: someone@example.com"
    result = strip_quoted_reply(text)
    assert "Thanks!" in result
    assert "someone@example.com" not in result


def test_strip_quoted_reply_cuts_at_leading_quote_block() -> None:
    text = "My reply here.\n> quoted line one\n> quoted line two"
    result = strip_quoted_reply(text)
    assert "My reply here." in result
    assert "quoted line" not in result


def test_strip_signature_cuts_at_dash_delimiter() -> None:
    text = "See you then.\n--\nJohn Doe\nSenior Engineer"
    result = strip_signature(text)
    assert "See you then." in result
    assert "Senior Engineer" not in result


def test_clean_email_body_truncates_long_text() -> None:
    body = "word " * 500
    result = clean_email_body(body, is_html=False, max_chars=800)
    assert len(result) <= 801  # +1 for the ellipsis char
    assert result.endswith("…")


def test_clean_email_body_full_pipeline() -> None:
    raw = (
        "<p>Can you review this before EOD?</p>"
        "<br>On Mon, Aug 11, 2026 John wrote:<br>&gt; original thread"
    )
    result = clean_email_body(raw, is_html=True)
    assert "review this before EOD" in result
    assert "original thread" not in result
