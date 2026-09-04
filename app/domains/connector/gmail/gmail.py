import httpx

from app.core.errors import ConnectorError, ConnectorErrorCode
from app.core.schemas import ExecutionResult, ExecutionStatus
from app.domains.connector.base import BaseConnector
from app.domains.oauth.service import get_access_token


class GmailConnector(BaseConnector):
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def execute(self, action: str, payload: dict) -> ExecutionResult:
        if action != "list_messages":
            return failed(
                payload["run_id"],
                payload["action_id"],
                "unsupported Gmail action",
            )

        max_results = payload.get("max_results", 10)
        params = {"maxResults": max_results}
        if payload.get("query"):
            params["q"] = payload["query"]

        try:
            data = await self._get("/users/me/messages", params)
        except httpx.HTTPStatusError as exc:
            return gmail_error(
                payload["run_id"],
                payload["action_id"],
                exc.response.status_code,
            )
        except httpx.TimeoutException:
            return failed(
                payload["run_id"],
                payload["action_id"],
                "Gmail request timed out",
                ConnectorErrorCode.TIMEOUT,
                retryable=True,
            )
        except httpx.HTTPError:
            return failed(
                payload["run_id"],
                payload["action_id"],
                "Gmail is unavailable",
                ConnectorErrorCode.UNAVAILABLE,
                retryable=True,
            )

        messages = data.get("messages", [])
        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="gmail",
            status=ExecutionStatus.SUCCESS,
            result_summary=f"Fetched {len(messages)} Gmail message(s)",
            data={
                "messages": messages,
                "result_size_estimate": data.get("resultSizeEstimate", len(messages)),
            },
        )

    async def _get(self, path: str, params: dict) -> dict:
        if self._client is not None:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()

        headers = {}
        token = await get_access_token("gmail")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(
            base_url="https://gmail.googleapis.com/gmail/v1",
            headers=headers,
            timeout=10,
        ) as client:
            response = await client.get(path, params=params)
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
        executor="gmail",
        status=ExecutionStatus.FAILED,
        result_summary=message,
        error=ConnectorError(code=code, message=message, retryable=retryable).model_dump(
            mode="json"
        ),
    )


def gmail_error(run_id: str, action_id: str, status_code: int) -> ExecutionResult:
    if status_code == 401:
        return failed(run_id, action_id, "Gmail authentication failed", ConnectorErrorCode.AUTH)
    if status_code == 403:
        return failed(
            run_id,
            action_id,
            "Gmail permission or rate limit failure",
            ConnectorErrorCode.PERMISSION,
        )
    if status_code == 404:
        return failed(run_id, action_id, "Gmail resource not found", ConnectorErrorCode.NOT_FOUND)
    return failed(
        run_id,
        action_id,
        f"Gmail returned HTTP {status_code}",
        ConnectorErrorCode.UNKNOWN,
    )
