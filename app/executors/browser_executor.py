from app.config.settings import get_settings
from app.core.schemas import ActionRequest, ExecutionResult, ExecutionStatus

# Steps with no action-list equivalent: they just navigate + snapshot.
_NAVIGATE_ONLY = {"BROWSER_OPEN", "BROWSER_SNAPSHOT"}


class BrowserExecutor:
    """
    Single-step browser action executor.

    Drives the same Playwright pipeline as the multi-step plan endpoint
    (``POST /api/v1/chat/execute`` -> ``run_browser_prototype_agent``), just
    for a single :class:`ActionRequest`. Calls the pipeline's inner
    ``_execute_with_browser`` directly rather than the full agent function,
    since guardrail decision + audit write already happened one level up
    (``ExecutionRouter`` is invoked from ``run_guarded_action``) and running
    them again here would double them.
    """

    async def execute(self, action: ActionRequest) -> ExecutionResult:
        # Local import: app.domains.agent.services.__init__ pulls in
        # guarded_execution -> app.executors, so a module-level import here
        # would be circular.
        from app.domains.agent.services.browser_prototype_agent import (
            BROWSER_ACTION_TYPE_MAP,
            _error_payload,
            _execute_with_browser,
            plan_step_to_browser_action,
        )

        url = action.payload.get("url") or action.target
        if not url or not isinstance(url, str):
            return self._failed(action, "missing browser target url")

        if action.action_type not in _NAVIGATE_ONLY and action.action_type not in BROWSER_ACTION_TYPE_MAP:
            return self._failed(action, f"unsupported browser action: {action.action_type}")

        browser_action = plan_step_to_browser_action(
            {"action_type": action.action_type, "payload": action.payload}
        )
        settings = get_settings()

        try:
            return await _execute_with_browser(
                request=action,
                url=url,
                actions=[browser_action] if browser_action else [],
                timeout_ms=action.payload.get("timeout_ms", 15_000),
                wait_until=action.payload.get("wait_until", "domcontentloaded"),
                settle_ms=action.payload.get("settle_ms", settings.BROWSER_SETTLE_MS),
            )
        except Exception as exc:
            return self._failed(action, "browser action failed", error=_error_payload(exc))

    @staticmethod
    def _failed(action: ActionRequest, summary: str, error: dict | None = None) -> ExecutionResult:
        return ExecutionResult(
            run_id=action.run_id,
            action_id=action.action_id,
            executor="browser",
            status=ExecutionStatus.FAILED,
            result_summary=summary,
            error=error or {"code": "INVALID_BROWSER_ACTION", "message": summary},
        )
