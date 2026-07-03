from app.core.schemas import ActionRequest, ExecutionResult, ExecutionStatus, new_id


class BrowserExecutor:
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

        return ExecutionResult(
            run_id=action.run_id,
            action_id=action.action_id,
            executor="browser",
            status=ExecutionStatus.SKIPPED,
            result_summary=f"browser action skeleton only: {action.action_type}",
        )
