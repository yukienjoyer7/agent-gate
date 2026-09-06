"""Canonical payload contract for Google Calendar event creation.

The planner, ActionRequest boundary, executor, and connector all use this
module.  ``start_time`` and ``end_time`` are intentionally the only supported
legacy aliases; all downstream code sees the canonical ``start`` and ``end``
keys.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CREATE_EVENT_REQUIRED_FIELDS = ("summary", "start", "end")


class CalendarPayloadValidationError(ValueError):
    """Safe validation error suitable for returning to an API caller."""


def normalize_create_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy using the canonical Calendar ``create_event`` keys.

    Canonical values take precedence.  The two documented legacy aliases are
    accepted for old planner output, then removed so approval, execution, and
    traces do not disagree about the payload shape.
    """
    normalized = dict(payload)
    for canonical, legacy in (("start", "start_time"), ("end", "end_time")):
        if not normalized.get(canonical) and normalized.get(legacy):
            normalized[canonical] = normalized[legacy]
        normalized.pop(legacy, None)
    return normalized


def missing_create_event_fields(payload: Mapping[str, Any]) -> list[str]:
    """List missing required canonical fields after legacy normalization."""
    normalized = normalize_create_event_payload(payload)
    return [field for field in CREATE_EVENT_REQUIRED_FIELDS if not normalized.get(field)]


def validate_create_event_payload(
    payload: Mapping[str, Any], default_timezone: str
) -> dict[str, Any]:
    """Validate and prepare a canonical payload for the Calendar API.

    Naive ISO 8601 datetimes are interpreted in the configured default
    timezone.  Datetimes that carry an offset are converted to the requested
    event timezone, so the serialized datetime and Google ``timeZone`` value
    always describe the same instant.
    """
    normalized = normalize_create_event_payload(payload)
    missing = missing_create_event_fields(normalized)
    if missing:
        raise CalendarPayloadValidationError(f"missing required fields: {', '.join(missing)}")

    timezone = str(normalized.get("timezone") or default_timezone).strip()
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise CalendarPayloadValidationError("invalid calendar timezone") from exc

    start = _parse_datetime(normalized["start"], "start")
    end = _parse_datetime(normalized["end"], "end")
    start = _in_timezone(start, zone)
    end = _in_timezone(end, zone)
    if end <= start:
        raise CalendarPayloadValidationError("end must be after start")

    normalized["start"] = start.isoformat()
    normalized["end"] = end.isoformat()
    normalized["timezone"] = timezone
    return normalized


def _parse_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise CalendarPayloadValidationError(f"invalid {field} datetime; expected ISO 8601")
    text = value.strip()
    # A date without a time parses to midnight in ``fromisoformat``. Treating
    # it as an event time would fabricate information the user did not give.
    if "T" not in text:
        raise CalendarPayloadValidationError(f"invalid {field} datetime; expected ISO 8601")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise CalendarPayloadValidationError(
            f"invalid {field} datetime; expected ISO 8601"
        ) from exc


def _in_timezone(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)
