"""Unit tests for backend/redaction.py's regex pass.

_local_model_confirm (the second pass) needs a running local model and is
covered indirectly via backend/tests/test_llm_client.py's redaction-gateway
tests, which patch it out. This file is about the deterministic regex pass
in isolation: what it catches, and — just as important for a dev-tooling
product where users constantly paste hashes/UUIDs/URLs — what it must not
false-positive on.
"""

from backend.redaction import _regex_redact


def test_redacts_openai_style_key():
    text = f"my key is sk-{'a' * 20}"
    redacted, mapping = _regex_redact(text)
    assert "sk-" not in redacted
    assert len(mapping) == 1
    assert list(mapping.values())[0] == f"sk-{'a' * 20}"


def test_redacts_labeled_password():
    redacted, mapping = _regex_redact("пароль: hunter2secret")
    assert "hunter2secret" not in redacted
    assert mapping


def test_redacts_generic_opaque_mixed_case_token():
    token = "Kx7aB3cD9eF1gH5iJ7kL9mN1oP3qR5sT7"
    redacted, mapping = _regex_redact(f"here is a token: {token}")
    assert token not in redacted
    assert mapping


def test_does_not_redact_git_commit_sha():
    text = "commit abc123def456abc123def456abc123def456ab fixed the bug"
    redacted, mapping = _regex_redact(text)
    assert redacted == text
    assert mapping == {}


def test_does_not_redact_uuid():
    text = "see uuid 550e8400-e29b-41d4-a716-446655440000 in the logs"
    redacted, mapping = _regex_redact(text)
    assert redacted == text
    assert mapping == {}


def test_does_not_redact_hash_shaped_url_path_segment():
    text = "https://example.com/path/9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e/file.js"
    redacted, mapping = _regex_redact(text)
    assert redacted == text
    assert mapping == {}


def test_uppercase_hex_hash_also_exempted():
    text = "checksum ABCDEF1234567890ABCDEF1234567890"
    redacted, mapping = _regex_redact(text)
    assert redacted == text
    assert mapping == {}


def test_redacts_luhn_valid_card_number():
    redacted, mapping = _regex_redact("card 4111111111111111 on file")
    assert "4111111111111111" not in redacted
    assert mapping


def test_does_not_redact_luhn_invalid_digit_run():
    """A 13-19 digit run that isn't a real card number (order id, timestamp,
    tracking number) must survive — it was never a secret, and the external
    model needs the real value to be useful."""
    text = "order id 20260813123456789"
    redacted, mapping = _regex_redact(text)
    assert redacted == text
    assert mapping == {}
