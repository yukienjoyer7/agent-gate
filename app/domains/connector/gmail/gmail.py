from app.core.schemas import ExecutionResult, ExecutionStatus
from app.domains.connector.base import BaseConnector


class GmailConnector(BaseConnector):
    async def execute(self, action: str, payload: dict) -> ExecutionResult:
        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="gmail",
            status=ExecutionStatus.SUCCESS,
            result_summary=f"Mock Gmail {action} completed",
            data={"mode": "mock", "action": action, "query": payload.get("query", "")},
        )
