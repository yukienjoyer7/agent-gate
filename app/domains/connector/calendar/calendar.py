import httpx

from app.core.errors import ConnectorError, ConnectorErrorCode
from app.core.schemas import ExecutionResult, ExecutionStatus
from app.domains.connector.base import BaseConnector
from app.domains.oauth.service import get_access_token


class CalendarConnector(BaseConnector):
    """Google Calendar connector.

    Supports ``list_events`` (read) and ``create_event`` (write), authenticated
    with the ``calendar`` OAuth provider (full calendar scope).
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def execute(self, action: str, payload: dict) -> ExecutionResult:
        if action == "list_events":
            return await self._list_events(payload)
        if action == "create_event":
            return await self._create_event(payload)
        return failed(
            payload["run_id"],
            payload["action_id"],
            "unsupported Calendar action",
        )

    async def _list_events(self, payload: dict) -> ExecutionResult:
        calendar_id = payload.get("calendar_id", "primary")
        params = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": payload.get("max_results", 10),
        }
        if payload.get("time_min"):
            params["timeMin"] = payload["time_min"]
        if payload.get("time_max"):
            params["timeMax"] = payload["time_max"]
        if payload.get("query"):
            params["q"] = payload["query"]

        try:
            data = await self._get(f"/calendars/{calendar_id}/events", params)
        except httpx.HTTPStatusError as exc:
            return calendar_error(
                payload["run_id"],
                payload["action_id"],
                exc.response.status_code,
            )
        except httpx.TimeoutException:
            return failed(
                payload["run_id"],
                payload["action_id"],
                "Calendar request timed out",
                ConnectorErrorCode.TIMEOUT,
                retryable=True,
            )
        except httpx.HTTPError:
            return failed(
                payload["run_id"],
                payload["action_id"],
                "Calendar is unavailable",
                ConnectorErrorCode.UNAVAILABLE,
                retryable=True,
            )

        events = data.get("items", [])
        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="calendar",
            status=ExecutionStatus.SUCCESS,
            result_summary=f"Fetched {len(events)} Calendar event(s)",
            data={"events": events},
        )

    async def _create_event(self, payload: dict) -> ExecutionResult:
        required = ("summary", "start", "end")
        missing = [f for f in required if not payload.get(f)]
        if missing:
            return failed(
                payload["run_id"],
                payload["action_id"],
                f"missing required fields: {', '.join(missing)}",
            )

        calendar_id = payload.get("calendar_id", "primary")
        body: dict = {
            "summary": payload["summary"],
            "start": {"dateTime": payload["start"], "timeZone": payload.get("timezone", "UTC")},
            "end": {"dateTime": payload["end"], "timeZone": payload.get("timezone", "UTC")},
        }
        if payload.get("description"):
            body["description"] = payload["description"]
        if payload.get("location"):
            body["location"] = payload["location"]

        try:
            data = await self._post(f"/calendars/{calendar_id}/events", body)
        except httpx.HTTPStatusError as exc:
            return calendar_error(
                payload["run_id"],
                payload["action_id"],
                exc.response.status_code,
            )
        except httpx.TimeoutException:
            return failed(
                payload["run_id"],
                payload["action_id"],
                "Calendar request timed out",
                ConnectorErrorCode.TIMEOUT,
                retryable=True,
            )
        except httpx.HTTPError:
            return failed(
                payload["run_id"],
                payload["action_id"],
                "Calendar is unavailable",
                ConnectorErrorCode.UNAVAILABLE,
                retryable=True,
            )

        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="calendar",
            status=ExecutionStatus.SUCCESS,
            result_summary=f"Created Calendar event: {data.get('summary', payload['summary'])}",
            data={"event": data},
        )

    async def _get(self, path: str, params: dict) -> dict:
        if self._client is not None:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()

        headers = {}
        token = await get_access_token("calendar")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(
            base_url="https://www.googleapis.com/calendar/v3",
            headers=headers,
            timeout=10,
        ) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    async def _post(self, path: str, json_body: dict) -> dict:
        if self._client is not None:
            response = await self._client.post(path, json=json_body)
            response.raise_for_status()
            return response.json()

        headers = {}
        token = await get_access_token("calendar")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(
            base_url="https://www.googleapis.com/calendar/v3",
            headers=headers,
            timeout=10,
        ) as client:
            response = await client.post(path, json=json_body)
            response.raise_for_status()
            return response.json()


def failed(
    run_id: str,
    action_id: str,
    message: str,
    code: ConnectorErrorCode = ConnectorErrorCode.VALIDATION,
    retryable: bool = False,
) -> ExecutionResult:
    return ExecutionResult(
        run_id=run_id,
        action_id=action_id,
        executor="calendar",
        status=ExecutionStatus.FAILED,
        result_summary=message,
        error=ConnectorError(code=code, message=message, retryable=retryable).model_dump(
            mode="json"
        ),
    )


def calendar_error(run_id: str, action_id: str, status_code: int) -> ExecutionResult:
    if status_code == 401:
        return failed(run_id, action_id, "Calendar authentication failed", ConnectorErrorCode.AUTH)
    if status_code == 403:
        return failed(
            run_id,
            action_id,
            "Calendar permission or rate limit failure",
            ConnectorErrorCode.PERMISSION,
        )
    if status_code == 404:
        return failed(run_id, action_id, "Calendar resource not found", ConnectorErrorCode.NOT_FOUND)
    return failed(
        run_id,
        action_id,
        f"Calendar returned HTTP {status_code}",
        ConnectorErrorCode.UNKNOWN,
    )