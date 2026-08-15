from app.core.schemas import (
    ActionRequest,
    Decision,
    DecisionResponse,
    ExecutionResult,
    ExecutionStatus,
)
from app.executors.api_executor import APIExecutor
from app.executors.browser_executor import BrowserExecutor


def decision_to_execution_status(decision: Decision) -> ExecutionStatus | None:
    """Map a guardrail Decision to the ExecutionStatus that represents its
    non-executing state in the pipeline.

    Returns ``None`` for ``ALLOW`` (and for decisions that have been granted
    the transition needed to execute, e.g. approved / sanitized), meaning the
    action may proceed to execution.
    """
    return {
        Decision.BLOCK: ExecutionStatus.BLOCKED,
        Decision.NEED_APPROVAL: ExecutionStatus.PENDING_APPROVAL,
        Decision.SANITIZE: ExecutionStatus.SANITIZED,
        Decision.ASK_USER: ExecutionStatus.WAITING_USER,
    }.get(decision)


class ExecutionRouter:
    def __init__(self) -> None:
        self.api = APIExecutor()
        self.browser = BrowserExecutor()

    async def route(
        self,
        action: ActionRequest,
        decision: DecisionResponse,
        *,
        approved: bool = False,
        use_sanitized: bool = False,
    ) -> ExecutionResult:
        """Route an action to execution only after the decision's required
        transition has occurred:

        - ALLOW          -> execute immediately
        - BLOCK          -> never execute
        - NEED_APPROVAL  -> execute only after approval (``approved=True``)
        - SANITIZE       -> execute only after sanitization + resulting
                            policy/confirmation state (``use_sanitized=True``,
                            with the sanitized payload substituted)
        - ASK_USER       -> never execute; clarification is required first
        """
        if decision.decision == Decision.BLOCK:
            return skipped(action, ExecutionStatus.BLOCKED, "blocked by guardrail")

        if decision.decision == Decision.ASK_USER:
            return skipped(
                action,
                ExecutionStatus.WAITING_USER,
                "clarification required from user before execution",
            )

        if decision.decision == Decision.NEED_APPROVAL and not approved:
            return skipped(action, ExecutionStatus.PENDING_APPROVAL, "pending approval")

        if decision.decision == Decision.SANITIZE and not use_sanitized:
            return skipped(
                action,
                ExecutionStatus.SANITIZED,
                "sanitized payload ready; awaiting confirmation before execution",
            )

        # Execution is permitted. For SANITIZE the payload must be the
        # sanitized one — the original unsafe payload is never executed.
        if (
            use_sanitized
            and decision.decision == Decision.SANITIZE
            and decision.sanitized_payload is not None
        ):
            action = action.model_copy(update={"payload": decision.sanitized_payload})

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
