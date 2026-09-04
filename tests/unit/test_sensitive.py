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


def test_angle_bracket_placeholder_flagged() -> None:
    fields = detect_sensitive_fields({"value": "<password>"})
    assert fields == [{"key": "value", "label": "Password"}]


def test_square_bracket_placeholder_flagged() -> None:
    fields = detect_sensitive_fields({"value": "[password]"})
    assert fields == [{"key": "value", "label": "Password"}]


def test_single_brace_placeholder_flagged() -> None:
    """Nvidia NIM emits single-brace {username} instead of {{username}}."""
    fields = detect_sensitive_fields({"value": "{username}"})
    assert fields == [{"key": "value", "label": "Username"}]

    fields = detect_sensitive_fields({"value": "{password}"})
    assert fields == [{"key": "value", "label": "Password"}]


def test_angle_bracket_username_flagged() -> None:
    fields = detect_sensitive_fields({"value": "<username>"})
    assert fields == [{"key": "value", "label": "Username"}]


def test_element_id_is_descriptive_not_flagged() -> None:
    """element_id carries the DOM id of the field, not a fill-in value."""
    payload = {"element_id": "username", "label": "Username", "value": "{username}"}
    fields = detect_sensitive_fields(payload, action_type="BROWSER_TYPE")
    assert fields == [{"key": "value", "label": "Username"}]


def test_filled_login_payload_not_flagged() -> None:
    payload = {"element_id": "username", "label": "Username", "value": "standard_user"}
    assert detect_sensitive_fields(payload, action_type="BROWSER_TYPE") == []


def test_your_password_here_flagged() -> None:
    fields = detect_sensitive_fields({"value": "YOUR_PASSWORD_HERE"})
    assert fields == [{"key": "value", "label": "Password"}]


def test_your_email_here_flagged() -> None:
    fields = detect_sensitive_fields({"value": "your email here"})
    assert fields == [{"key": "value", "label": "Email"}]


def test_bare_sensitive_word_flagged() -> None:
    fields = detect_sensitive_fields({"value": "password"})
    assert fields == [{"key": "value", "label": "Password"}]


def test_empty_value_on_type_action_flagged() -> None:
    fields = detect_sensitive_fields({"value": ""}, action_type="BROWSER_TYPE")
    assert fields == [{"key": "value", "label": "Value"}]


def test_empty_value_on_click_not_flagged() -> None:
    assert detect_sensitive_fields({"value": ""}, action_type="BROWSER_CLICK") == []
    assert detect_sensitive_fields({"value": ""}) == []


def test_real_values_not_flagged() -> None:
    assert detect_sensitive_fields({"value": "123456"}) == []
    assert detect_sensitive_fields({"value": "jerome-pw-9x"}) == []


def test_is_sensitive_key() -> None:
    assert is_sensitive_key("password")
    assert is_sensitive_key("API_TOKEN")
    assert is_sensitive_key("client_secret")
    assert not is_sensitive_key("label")
    assert not is_sensitive_key("value")
