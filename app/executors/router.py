from app.core.schemas import ActionRequest, Decision, DecisionResponse, ExecutionResult, ExecutionStatus
from app.executors.api_executor import APIExecutor
from app.executors.browser_executor import BrowserExecutor


class ExecutionRouter:
    def __init__(self) -> None:
        self.api = APIExecutor()
        self.browser = BrowserExecutor()

    async def route(self, action: ActionRequest, decision: DecisionResponse) -> ExecutionResult:
        if decision.decision == Decision.BLOCK:
            return skipped(action, ExecutionStatus.BLOCKED, "blocked by guardrail")
        if decision.decision == Decision.NEED_APPROVAL:
            return skipped(action, ExecutionStatus.PENDING_APPROVAL, "pending approval")
        if action.target_system == "browser" or action.action_type.startswith("BROWSER_"):
            return await self.browser.execute(action)
        return await self.api.execute(action)


def skipped(action: ActionRequest, status: ExecutionStatus, summary: str) -> ExecutionResult:
    return ExecutionResult(
        run_id=action.run_id,
        action_id=action.action_id,
        executor="router",
        status=status,
        result_summary=summary,
    )
