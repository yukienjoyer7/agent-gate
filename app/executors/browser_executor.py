from app.core.schemas import ActionRequest, ExecutionResult, ExecutionStatus, new_id


class BrowserExecutor:
    """
    Single-step browser action executor.

    .. note::

       For **real** Playwright execution (open page, snapshot DOM,
       resolve selectors, click／type), use the multi-step plan endpoint:

       ``POST /api/v1/chat/plan``   – plan only
       ``POST /api/v1/chat/execute`` – plan + execute through
       :func:`app.domains.agent.services.browser_prototype_agent.run_browser_prototype_agent`

       This executor provides mock responses for development and
       testing.  Interactive actions (click, type, …) require the
       multi-step flow because they need:

       - A live Playwright session
       - A snapshot + selector-map built from the real DOM
       - Element resolution by label／role against the snapshot
       - Sequential execution in a single browser context
    """

    async def execute(self, action: ActionRequest) -> ExecutionResult:
        if action.action_type == "BROWSER_OPEN":
            return ExecutionResult(
                run_id=action.run_id,
                action_id=action.action_id,
                executor="browser",
                status=ExecutionStatus.SUCCESS,
                result_summary=f"Opened {action.payload.get('url', action.target)}",
                data={"url": action.payload.get("url", action.target)},
            )

        if action.action_type == "BROWSER_SNAPSHOT":
            snapshot_id = new_id("snap")
            return ExecutionResult(
                run_id=action.run_id,
                action_id=action.action_id,
                executor="browser",
                status=ExecutionStatus.SUCCESS,
                result_summary="Created mock browser snapshot",
                data={
                    "snapshot_id": snapshot_id,
                    "url": action.payload.get("url", action.target),
                    "title": action.payload.get("title", "Demo page"),
                    "elements": [
                        {
                            "snapshot_id": snapshot_id,
                            "element_id": "e_001",
                            "role": "button",
                            "label": "Continue",
                            "text": "Continue",
                            "risk_hint": "unknown",
                        }
                    ],
                },
            )

        if action.action_type in (
            "BROWSER_CLICK",
            "BROWSER_TYPE",
            "BROWSER_SELECT",
            "BROWSER_SUBMIT",
            "BROWSER_SCREENSHOT",
            "BROWSER_SCROLL",
        ):
            return ExecutionResult(
                run_id=action.run_id,
                action_id=action.action_id,
                executor="browser",
                status=ExecutionStatus.SKIPPED,
                result_summary=(
                    f"Interactive browser action ({action.action_type}) requires a real "
                    f"Playwright session. Use POST /api/v1/chat/execute with the "
                    f"multi-step plan endpoint."
                ),
                data={
                    "action_type": action.action_type,
                    "hint": "use POST /api/v1/chat/execute for real browser execution",
                },
            )

        return ExecutionResult(
            run_id=action.run_id,
            action_id=action.action_id,
            executor="browser",
            status=ExecutionStatus.FAILED,
            result_summary=f"unsupported browser action: {action.action_type}",
        )
