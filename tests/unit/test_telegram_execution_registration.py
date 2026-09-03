import asyncio

from app.core.action_request import build_action_request
from app.core.schemas import Decision, DecisionResponse, ExecutionResult, ExecutionStatus
from app.domains.connector.telegram import TelegramConnector
from app.executors.api_executor import APIExecutor
from app.executors.router import ExecutionRouter


def test_api_executor_registers_telegram_connector() -> None:
    executor = APIExecutor()

    assert isinstance(executor.connectors["telegram"], TelegramConnector)


def test_unknown_telegram_action_does_not_crash() -> None:
    action = build_action_request(
        {
            "action_type": "API_CALL",
            "target_system": "telegram",
            "target": "123",
            "domain": "productivity",
            "risk_hint": "unknown",
            "payload": {"action": "unknown"},
        }
    )

    result = asyncio.run(APIExecutor().execute(action))

    assert result.status == "FAILED"
    assert result.error["code"] == "VALIDATION"


def test_telegram_connector_stays_behind_execution_router_approval_gate() -> None:
    router = ExecutionRouter()
    calls: list = []

    async def fake_execute(action):
        calls.append(action)
        return ExecutionResult(
            run_id=action.run_id,
            action_id=action.action_id,
            executor="api",
            status=ExecutionStatus.SUCCESS,
        )

    router.api.execute = fake_execute
    action = build_action_request(
        {
            "action_type": "API_CALL",
            "target_system": "telegram",
            "target": "123",
            "domain": "productivity",
            "risk_hint": "external_send",
            "payload": {"action": "send_message", "chat_id": "123", "text": "hello"},
        }
    )
    decision = DecisionResponse(
        run_id=action.run_id,
        action_id=action.action_id,
        decision=Decision.NEED_APPROVAL,
    )

    result = asyncio.run(router.route(action, decision))

    assert result.status == ExecutionStatus.PENDING_APPROVAL
    assert calls == []
