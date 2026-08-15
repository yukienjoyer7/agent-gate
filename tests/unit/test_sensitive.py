"""Tests for the "sanitize" field detector (app.domains.guardrail.sensitive)."""

from app.domains.guardrail.sensitive import detect_sensitive_fields, is_sensitive_key


def test_empty_password_flagged() -> None:
    fields = detect_sensitive_fields({"password": ""})
    assert fields == [{"key": "password", "label": "Password"}]


def test_none_secret_flagged() -> None:
    fields = detect_sensitive_fields({"api_key": None})
    assert fields[0]["key"] == "api_key"


def test_placeholder_flagged() -> None:
    fields = detect_sensitive_fields({"value": "{{password}}"})
    assert fields == [{"key": "value", "label": "Password"}]


def test_placeholder_with_spaces() -> None:
    fields = detect_sensitive_fields({"value": "{{ api_key }}"})
    assert fields == [{"key": "value", "label": "Api Key"}]


def test_filled_secret_not_flagged() -> None:
    assert detect_sensitive_fields({"password": "hunter2"}) == []


def test_normal_payload_not_flagged() -> None:
    assert detect_sensitive_fields({"label": "Search", "value": "jerome"}) == []


def test_answered_keys_skipped() -> None:
    fields = detect_sensitive_fields({"password": "", "api_key": ""}, answered={"password"})
    assert fields == [{"key": "api_key", "label": "Api Key"}]


def test_is_sensitive_key() -> None:
    assert is_sensitive_key("password")
    assert is_sensitive_key("API_TOKEN")
    assert is_sensitive_key("client_secret")
    assert not is_sensitive_key("label")
    assert not is_sensitive_key("value")
