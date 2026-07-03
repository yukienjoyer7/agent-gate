from app.core.schemas import ExecutionResult, ExecutionStatus
from app.domains.connector.base import BaseConnector


class GitHubConnector(BaseConnector):
    async def execute(self, action: str, payload: dict) -> ExecutionResult:
        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="github",
            status=ExecutionStatus.SUCCESS,
            result_summary=f"Mock GitHub {action} completed",
            data={"mode": "mock", "action": action, "repo": payload.get("repo", "")},
        )
