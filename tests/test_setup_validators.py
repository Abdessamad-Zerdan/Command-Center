from command_center.setup_wizard import validators


def test_valid_groq_key_passes() -> None:
    assert validators.validate_groq_key("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") is None


def test_empty_groq_key_fails() -> None:
    error = validators.validate_groq_key("   ")
    assert error is not None
    assert "Enter your Groq API key" in error


def test_wrong_prefix_fails() -> None:
    error = validators.validate_groq_key("sk-not-a-groq-key-1234567890")
    assert error is not None
    assert "gsk_" in error


def test_too_short_fails() -> None:
    error = validators.validate_groq_key("gsk_short")
    assert error is not None
