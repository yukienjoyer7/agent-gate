from importlib import import_module

from app.core.errors import ConnectorError
from app.core.schemas import ActionRequest, ExecutionResult, ExecutionStatus
from app.domains.connector.base import BaseConnector
from app.domains.connector.calendar.contract import normalize_create_event_payload

_CONNECTORS = {
    "local_file": ("app.domains.connector.filesystem", "LocalFileConnector"),
    "github": ("app.domains.connector.github", "GitHubConnector"),
    "gmail": ("app.domains.connector.gmail", "GmailConnector"),
    "stripe": ("app.domains.connector.stripe", "StripeConnector"),
    "telegram": ("app.domains.connector.telegram", "TelegramConnector"),
    "calendar": ("app.domains.connector.calendar", "CalendarConnector"),
}


class APIExecutor:
    def __init__(self, connectors: dict[str, BaseConnector] | None = None) -> None:
        self.connectors = connectors if connectors is not None else {}
        self._use_defaults = connectors is None

    def connector_for(self, target_system: str) -> BaseConnector | None:
        connector = self.connectors.get(target_system)
        if connector is None and self._use_defaults and target_system in _CONNECTORS:
            module_name, class_name = _CONNECTORS[target_system]
            connector = getattr(import_module(module_name), class_name)()
            self.connectors[target_system] = connector
        return connector

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

        connector = self.connector_for(action.target_system)
        if connector is None:
            return ExecutionResult(
                run_id=action.run_id,
                action_id=action.action_id,
                executor="api",
                status=ExecutionStatus.FAILED,
                result_summary=f"unknown connector: {action.target_system}",
                error=ConnectorError.validation("unknown connector").model_dump(mode="json"),
            )
        payload = {
            **action.payload,
            "run_id": action.run_id,
            "action_id": action.action_id,
            "target": action.target,
        }
        if action.target_system == "calendar" and connector_action == "create_event":
            payload = normalize_create_event_payload(payload)
        return await connector.execute(connector_action, payload)
