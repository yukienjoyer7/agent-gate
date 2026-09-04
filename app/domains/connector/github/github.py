import httpx

from app.core.errors import ConnectorError, ConnectorErrorCode
from app.core.schemas import ExecutionResult, ExecutionStatus
from app.domains.connector.base import BaseConnector
from app.domains.oauth.service import get_access_token


class GitHubConnector(BaseConnector):
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def execute(self, action: str, payload: dict) -> ExecutionResult:
        if action != "repo_metadata":
            return failed(
                payload["run_id"],
                payload["action_id"],
                "unsupported GitHub action",
            )

        owner, repo = parse_repo(payload)
        if not owner or not repo:
            return failed(
                payload["run_id"],
                payload["action_id"],
                "owner and repo are required",
            )

        try:
            data = await self._get(f"/repos/{owner}/{repo}")
        except httpx.HTTPStatusError as exc:
            return github_error(
                payload["run_id"],
                payload["action_id"],
                exc.response.status_code,
            )
        except httpx.TimeoutException:
            return failed(
                payload["run_id"],
                payload["action_id"],
                "GitHub request timed out",
                ConnectorErrorCode.TIMEOUT,
                retryable=True,
            )
        except httpx.HTTPError:
            return failed(
                payload["run_id"],
                payload["action_id"],
                "GitHub is unavailable",
                ConnectorErrorCode.UNAVAILABLE,
                retryable=True,
            )

        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="github",
            status=ExecutionStatus.SUCCESS,
            result_summary=f"Fetched GitHub metadata for {data['full_name']}",
            data={
                "id": data["id"],
                "full_name": data["full_name"],
                "private": data["private"],
                "default_branch": data["default_branch"],
                "html_url": data["html_url"],
                "description": data.get("description"),
                "stars": data.get("stargazers_count", 0),
                "forks": data.get("forks_count", 0),
                "open_issues": data.get("open_issues_count", 0),
            },
        )

    async def _get(self, path: str) -> dict:
        if self._client is not None:
            response = await self._client.get(path)
            response.raise_for_status()
            return response.json()

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = await get_access_token("github")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=headers,
            timeout=10,
        ) as client:
            response = await client.get(path)
            response.raise_for_status()
            return response.json()


def parse_repo(payload: dict) -> tuple[str, str]:
    owner = payload.get("owner", "")
    repo = payload.get("repo", "")
    target = payload.get("target", "")
    if (not owner or not repo) and isinstance(target, str) and "/" in target:
        owner, repo = target.split("/", 1)
    return owner.strip(), repo.strip()


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
        executor="github",
        status=ExecutionStatus.FAILED,
        result_summary=message,
        error=ConnectorError(code=code, message=message, retryable=retryable).model_dump(
            mode="json"
        ),
    )


def github_error(run_id: str, action_id: str, status_code: int) -> ExecutionResult:
    if status_code == 401:
        return failed(run_id, action_id, "GitHub authentication failed", ConnectorErrorCode.AUTH)
    if status_code == 403:
        return failed(
            run_id,
            action_id,
            "GitHub permission or rate limit failure",
            ConnectorErrorCode.PERMISSION,
        )
    if status_code == 404:
        return failed(run_id, action_id, "GitHub repository not found", ConnectorErrorCode.NOT_FOUND)
    return failed(
        run_id,
        action_id,
        f"GitHub returned HTTP {status_code}",
        ConnectorErrorCode.UNKNOWN,
    )
