import asyncio

from app.core.action_request import build_action_request
from app.core.schemas import (
    AuditEvent,
    Decision,
    DecisionResponse,
    ExecutionResult,
    ExecutionStatus,
)
from app.domains.agent.services import guarded_execution
from app.executors.api_executor import APIExecutor
from app.executors.router import ExecutionRouter


def _legacy_calendar_action():
    return build_action_request(
        {
            "run_id": "run_calendar",
            "action_id": "act_calendar",
            "action_type": "API_CALL",
            "target_system": "calendar",
            "target": "primary",
            "risk_hint": "unknown",
            "payload": {
                "action": "create_event",
                "summary": "meeting laplace #2",
                "start_time": "2026-09-13T18:00:00+07:00",
                "end_time": "2026-09-13T19:00:00+07:00",
            },
        }
    )


def test_action_request_normalizes_legacy_calendar_fields_and_requires_approval() -> None:
    action = _legacy_calendar_action()

    assert action.payload == {
        "action": "create_event",
        "summary": "meeting laplace #2",
        "start": "2026-09-13T18:00:00+07:00",
        "end": "2026-09-13T19:00:00+07:00",
    }
    assert action.risk_hint == "external_send"
    assert action.domain == "productivity"


def test_api_executor_forwards_only_canonical_calendar_payload() -> None:
    captured: dict = {}

    class RecordingConnector:
        async def execute(self, action, payload):
            captured["action"] = action
            captured["payload"] = payload
            return ExecutionResult(
                run_id=payload["run_id"],
                action_id=payload["action_id"],
                executor="calendar",
                status=ExecutionStatus.SUCCESS,
            )

    executor = APIExecutor()
    executor.connectors["calendar"] = RecordingConnector()
    result = asyncio.run(executor.execute(_legacy_calendar_action()))

    assert result.status == ExecutionStatus.SUCCESS
    assert captured["action"] == "create_event"
    assert captured["payload"]["start"] == "2026-09-13T18:00:00+07:00"
    assert captured["payload"]["end"] == "2026-09-13T19:00:00+07:00"
    assert "start_time" not in captured["payload"]
    assert "end_time" not in captured["payload"]


def test_calendar_create_event_is_not_routed_before_approval() -> None:
    router = ExecutionRouter()
    calls: list = []

    async def fake_execute(action):
        calls.append(action)
        return ExecutionResult(
            run_id=action.run_id,
            action_id=action.action_id,
            executor="calendar",
            status=ExecutionStatus.SUCCESS,
        )

    router.api.execute = fake_execute
    action = _legacy_calendar_action()
    from app.domains.guardrail.decision import decide

    decision = decide(action)
    result = asyncio.run(router.route(action, decision))

    assert decision.decision == Decision.NEED_APPROVAL
    assert result.status == ExecutionStatus.PENDING_APPROVAL
    assert calls == []


def test_calendar_trace_and_audit_receive_canonical_payload(monkeypatch) -> None:
    class RecordingAudit:
        def __init__(self) -> None:
            self.request = None

        async def write(self, request, decision, execution, latency=None):
            self.request = request
            return AuditEvent(
                run_id=request.run_id,
                action_id=request.action_id,
                request_json={},
                decision_json={},
                execution_json=execution.model_dump(mode="json"),
                execution_status=execution.status,
            )

    class RecordingTrace:
        def __init__(self) -> None:
            self.trace = None

        def write(self, trace) -> None:
            self.trace = trace

    class SuccessfulRouter:
        async def route(self, action, decision):
            return ExecutionResult(
                run_id=action.run_id,
                action_id=action.action_id,
                executor="calendar",
                status=ExecutionStatus.SUCCESS,
            )

    audit = RecordingAudit()
    traces = RecordingTrace()
    action = _legacy_calendar_action()
    decision = DecisionResponse(
        run_id=action.run_id,
        action_id=action.action_id,
        decision=Decision.ALLOW,
    )
    monkeypatch.setattr(guarded_execution, "ExecutionRouter", SuccessfulRouter)

    asyncio.run(
        guarded_execution.run_guarded_action(
            {
                "run_id": action.run_id,
                "action_id": action.action_id,
                "action_type": "API_CALL",
                "target_system": "calendar",
                "target": "primary",
                "payload": {
                    "action": "create_event",
                    "summary": "meeting laplace #2",
                    "start_time": "2026-09-13T18:00:00+07:00",
                    "end_time": "2026-09-13T19:00:00+07:00",
                },
            },
            audit=audit,
            traces=traces,
            decision=decision,
        )
    )

    assert audit.request.payload == action.payload
    assert traces.trace.raw_tool_call["payload"] == action.payload
