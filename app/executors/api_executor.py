from app.core.errors import ConnectorError
from app.core.schemas import ActionRequest, ExecutionResult, ExecutionStatus
from app.domains.connector.filesystem import LocalFileConnector
from app.domains.connector.github import GitHubConnector
from app.domains.connector.gmail import GmailConnector


class APIExecutor:
    def __init__(self) -> None:
        self.connectors = {
            "local_file": LocalFileConnector(),
            "github": GitHubConnector(),
            "gmail": GmailConnector(),
        }

    async def execute(self, action: ActionRequest) -> ExecutionResult:
        connector_action = action.payload.get("action")
        if not connector_action:
            return ExecutionResult(
                run_id=action.run_id,
                action_id=action.action_id,
                executor="api",
                status=ExecutionStatus.FAILED,
                result_summary="missing connector action",
                error=ConnectorError.validation("missing connector action").model_dump(mode="json"),
            )

        connector = self.connectors.get(action.target_system)
        if connector is None:
            return ExecutionResult(
                run_id=action.run_id,
                action_id=action.action_id,
                executor="api",
                status=ExecutionStatus.FAILED,
                result_summary=f"unknown connector: {action.target_system}",
                error=ConnectorError.validation("unknown connector").model_dump(mode="json"),
            )
        payload = {"run_id": action.run_id, "action_id": action.action_id, **action.payload}
        return await connector.execute(connector_action, payload)
