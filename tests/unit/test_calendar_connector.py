import json

import asyncio

import httpx

from app.domains.connector.calendar.calendar import CalendarConnector


def test_calendar_list_events_uses_events_api():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/calendar/v3/calendars/primary/events"
        assert request.url.params["singleEvents"] == "true"
        assert request.url.params["orderBy"] == "startTime"
        assert request.url.params["q"] == "demo"
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": "ev1", "summary": "Standup", "start": {"dateTime": "2026-09-01T09:00:00Z"}},
                    {"id": "ev2", "summary": "Review", "start": {"dateTime": "2026-09-01T10:00:00Z"}},
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://www.googleapis.com/calendar/v3",
        ) as client:
            return await CalendarConnector(client).execute(
                "list_events",
                {"run_id": "run_1", "action_id": "act_1", "query": "demo"},
            )

    result = asyncio.run(run())

    assert result.status == "SUCCESS"
    assert len(result.data["events"]) == 2
    assert result.result_summary == "Fetched 2 Calendar event(s)"


def test_calendar_list_events_supports_custom_calendar_id_and_time_bounds():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/calendar/v3/calendars/coworking/events"
        assert request.url.params["timeMin"] == "2026-09-01T00:00:00Z"
        assert request.url.params["timeMax"] == "2026-09-02T00:00:00Z"
        return httpx.Response(200, json={"items": []})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://www.googleapis.com/calendar/v3",
        ) as client:
            return await CalendarConnector(client).execute(
                "list_events",
                {
                    "run_id": "run_1",
                    "action_id": "act_1",
                    "calendar_id": "coworking",
                    "time_min": "2026-09-01T00:00:00Z",
                    "time_max": "2026-09-02T00:00:00Z",
                },
            )

    result = asyncio.run(run())

    assert result.status == "SUCCESS"
    assert result.data["events"] == []


def test_calendar_list_events_maps_auth_failure():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(401)),
            base_url="https://www.googleapis.com/calendar/v3",
        ) as client:
            return await CalendarConnector(client).execute(
                "list_events",
                {"run_id": "run_1", "action_id": "act_1"},
            )

    result = asyncio.run(run())

    assert result.status == "FAILED"
    assert result.error["code"] == "AUTH"


def test_calendar_rejects_unsupported_action():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            base_url="https://www.googleapis.com/calendar/v3",
        ) as client:
            return await CalendarConnector(client).execute(
                "delete_event",
                {"run_id": "run_1", "action_id": "act_1"},
            )

    result = asyncio.run(run())

    assert result.status == "FAILED"
    assert result.result_summary == "unsupported Calendar action"

def test_calendar_create_event_posts_to_events_api():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/calendar/v3/calendars/primary/events"
        body = request.content
        assert b'"summary"' in body
        return httpx.Response(
            200,
            json={
                "id": "evt_123",
                "summary": "Team Standup",
                "htmlLink": "https://calendar.google.com/event?id=evt_123",
            },
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://www.googleapis.com/calendar/v3",
        ) as client:
            return await CalendarConnector(client).execute(
                "create_event",
                {
                    "run_id": "run_1",
                    "action_id": "act_1",
                    "summary": "Team Standup",
                    "start": "2026-09-07T10:00:00Z",
                    "end": "2026-09-07T10:30:00Z",
                },
            )

    result = asyncio.run(run())

    assert result.status == "SUCCESS"
    assert result.data["event"]["id"] == "evt_123"
    assert "Created Calendar event" in result.result_summary


def test_calendar_create_event_validates_required_fields():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            base_url="https://www.googleapis.com/calendar/v3",
        ) as client:
            return await CalendarConnector(client).execute(
                "create_event",
                {"run_id": "run_1", "action_id": "act_1"},
            )

    result = asyncio.run(run())

    assert result.status == "FAILED"
    assert "missing required fields" in result.result_summary


def test_calendar_create_event_includes_optional_fields():
    captured_body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"id": "evt_456", "summary": "Meeting"},
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://www.googleapis.com/calendar/v3",
        ) as client:
            return await CalendarConnector(client).execute(
                "create_event",
                {
                    "run_id": "run_1",
                    "action_id": "act_1",
                    "summary": "Meeting",
                    "start": "2026-09-07T14:00:00+07:00",
                    "end": "2026-09-07T15:00:00+07:00",
                    "timezone": "Asia/Jakarta",
                    "description": "Weekly sync",
                    "location": "Room A",
                },
            )

    result = asyncio.run(run())

    assert result.status == "SUCCESS"
    assert captured_body["description"] == "Weekly sync"
    assert captured_body["location"] == "Room A"
    assert captured_body["start"]["timeZone"] == "Asia/Jakarta"
    assert captured_body["end"]["timeZone"] == "Asia/Jakarta"