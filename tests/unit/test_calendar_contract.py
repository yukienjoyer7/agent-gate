import pytest

from app.domains.connector.calendar.contract import (
    CalendarPayloadValidationError,
    missing_create_event_fields,
    normalize_create_event_payload,
    validate_create_event_payload,
)


def test_canonical_create_event_fields_pass_through_unchanged() -> None:
    payload = {
        "action": "create_event",
        "summary": "Meeting",
        "start": "2026-09-13T18:00:00+07:00",
        "end": "2026-09-13T19:00:00+07:00",
    }

    assert normalize_create_event_payload(payload) == payload


def test_legacy_datetime_aliases_normalize_to_canonical_keys() -> None:
    normalized = normalize_create_event_payload(
        {
            "action": "create_event",
            "summary": "Meeting",
            "start_time": "2026-09-13T18:00:00+07:00",
            "end_time": "2026-09-13T19:00:00+07:00",
        }
    )

    assert normalized["start"] == "2026-09-13T18:00:00+07:00"
    assert normalized["end"] == "2026-09-13T19:00:00+07:00"
    assert "start_time" not in normalized
    assert "end_time" not in normalized


@pytest.mark.parametrize(
    ("payload", "missing"),
    [
        ({"summary": "Meeting", "start_time": "2026-09-13T18:00:00+07:00"}, ["end"]),
        ({"summary": "Meeting", "end_time": "2026-09-13T19:00:00+07:00"}, ["start"]),
    ],
)
def test_one_legacy_datetime_alias_does_not_fabricate_the_other(payload, missing) -> None:
    assert missing_create_event_fields(payload) == missing


@pytest.mark.parametrize(
    ("start", "end", "error"),
    [
        ("not-a-datetime", "2026-09-13T19:00:00+07:00", "invalid start datetime"),
        ("2026-09-13T18:00:00+07:00", "not-a-datetime", "invalid end datetime"),
        ("2026-09-13T19:00:00+07:00", "2026-09-13T18:00:00+07:00", "end must be after start"),
        ("2026-09-13T18:00:00+07:00", "2026-09-13T18:00:00+07:00", "end must be after start"),
        ("2026-09-13", "2026-09-13T19:00:00+07:00", "invalid start datetime"),
    ],
)
def test_invalid_or_non_positive_calendar_duration_is_rejected(start, end, error) -> None:
    with pytest.raises(CalendarPayloadValidationError, match=error):
        validate_create_event_payload(
            {"summary": "Meeting", "start": start, "end": end},
            "Asia/Jakarta",
        )


def test_naive_datetimes_use_the_configured_calendar_timezone() -> None:
    payload = validate_create_event_payload(
        {
            "summary": "Meeting",
            "start": "2026-09-13T18:00:00",
            "end": "2026-09-13T19:00:00",
        },
        "Asia/Jakarta",
    )

    assert payload["start"] == "2026-09-13T18:00:00+07:00"
    assert payload["end"] == "2026-09-13T19:00:00+07:00"
    assert payload["timezone"] == "Asia/Jakarta"
