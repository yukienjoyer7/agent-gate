from app.domains.guardrail.detectors import scan_and_sanitize


def test_clean_payload_is_unchanged():
    payload = {"action": "read", "path": "sample.txt"}
    sanitized, found = scan_and_sanitize(payload)

    assert sanitized == payload
    assert found == []


def test_email_is_detected_and_redacted():
    payload = {"note": "contact me at foo@example.com please"}
    sanitized, found = scan_and_sanitize(payload)

    assert found == ["EMAIL"]
    assert "foo@example.com" not in sanitized["note"]
    assert "[REDACTED_EMAIL]" in sanitized["note"]


def test_ssn_is_detected_and_redacted():
    payload = {"body": "SSN on file: 123-45-6789"}
    sanitized, found = scan_and_sanitize(payload)

    assert "SSN" in found
    assert "123-45-6789" not in sanitized["body"]


def test_credit_card_is_detected_and_redacted():
    payload = {"body": "card number 4111111111111111"}
    sanitized, found = scan_and_sanitize(payload)

    assert "CREDIT_CARD" in found
    assert "4111111111111111" not in sanitized["body"]


def test_api_key_is_detected_and_redacted():
    payload = {"body": "use sk-abcdefghijklmnopqrstuvwx to auth"}
    sanitized, found = scan_and_sanitize(payload)

    assert "API_KEY" in found
    assert "sk-abcdefghijklmnopqrstuvwx" not in sanitized["body"]


def test_multiple_entities_in_one_string_are_all_found():
    payload = {"body": "email foo@example.com and ssn 123-45-6789"}
    sanitized, found = scan_and_sanitize(payload)

    assert set(found) == {"EMAIL", "SSN"}
    assert "foo@example.com" not in sanitized["body"]
    assert "123-45-6789" not in sanitized["body"]


def test_nested_dict_is_scanned_recursively():
    payload = {"outer": {"inner": {"note": "email foo@example.com"}}}
    sanitized, found = scan_and_sanitize(payload)

    assert found == ["EMAIL"]
    assert "foo@example.com" not in sanitized["outer"]["inner"]["note"]


def test_list_values_are_scanned():
    payload = {"notes": ["clean value", "contact foo@example.com"]}
    sanitized, found = scan_and_sanitize(payload)

    assert found == ["EMAIL"]
    assert "foo@example.com" not in sanitized["notes"][1]
    assert sanitized["notes"][0] == "clean value"


def test_non_string_values_are_left_alone():
    payload = {"count": 5, "active": True, "ratio": 0.5, "tags": None}
    sanitized, found = scan_and_sanitize(payload)

    assert sanitized == payload
    assert found == []


def test_only_entity_type_is_recorded_never_raw_value():
    """
    Per the audit schema doc's Data Retention rule: only entity TYPES may
    be recorded, never the raw sensitive value itself.
    """
    payload = {"note": "foo@example.com"}
    _, found = scan_and_sanitize(payload)

    for entity in found:
        assert "@" not in entity
        assert "foo" not in entity
