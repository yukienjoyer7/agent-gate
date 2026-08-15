"""Tests for the five decision flows through ExecutionRouter + decide()."""

import asyncio

import pytest

from app.core.schemas import (
    ActionRequest,
    Decision,
    DecisionResponse,
    ExecutionResult,
    ExecutionStatus,
    RiskLevel,
)
from app.domains.guardrail.decision import decide
from app.executors import ExecutionRouter


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[ActionRequest] = []

    async def execute(self, action: ActionRequest) -> ExecutionResult:
        self.calls.append(action)
        return ExecutionResult(
            run_id=action.run_id,
            action_id=action.action_id,
            executor="fake",
            status=ExecutionStatus.SUCCESS,
            result_summary="ok",
        )


class _Router(ExecutionRouter):
    def __init__(self) -> None:
        self.api = _RecordingExecutor()
        self.browser = _RecordingExecutor()


def _decision(decision: Decision, **overrides) -> DecisionResponse:
    return DecisionResponse(
        run_id="run_test",
        action_id="act_test",
        decision=decision,
        risk_level=RiskLevel.LOW,
        **overrides,
    )


def _request(**overrides) -> ActionRequest:
    defaults = {
        "action_type": "API_CALL",
        "target_system": "gmail",
        "target": "john@example.com",
        "payload": {"action": "send_message"},
    }
    defaults.update(overrides)
    return ActionRequest(**defaults)


def test_allow_executes_and_returns_result():
    router = _Router()
    result = asyncio.run(router.route(_request(), _decision(Decision.ALLOW)))
    assert len(router.browser.calls) + len(router.api.calls) == 1
    assert result.status == ExecutionStatus.SUCCESS


def test_block_never_calls_executor():
    router = _Router()
    result = asyncio.run(router.route(_request(), _decision(Decision.BLOCK)))
    assert router.browser.calls == []
    assert router.api.calls == []
    assert result.status == ExecutionStatus.BLOCKED


def test_need_approval_not_executed_until_approved():
    router = _Router()
    request = _request()

    pending = asyncio.run(router.route(request, _decision(Decision.NEED_APPROVAL)))
    assert router.browser.calls == []
    assert router.api.calls == []
    assert pending.status == ExecutionStatus.PENDING_APPROVAL

    approved = asyncio.run(router.route(request, _decision(Decision.NEED_APPROVAL), approved=True))
    assert len(router.api.calls) == 1
    assert approved.status == ExecutionStatus.SUCCESS


def test_need_approval_rejected_never_calls_executor():
    router = _Router()
    result = asyncio.run(
        router.route(_request(), _decision(Decision.NEED_APPROVAL), approved=False)
    )
    assert router.browser.calls == []
    assert router.api.calls == []
    assert result.status == ExecutionStatus.PENDING_APPROVAL


def test_sanitize_detects_secret_and_generates_sanitized_payload():
    request = _request(
        payload={
            "action": "send_message",
            "message": "My API key is sk-1234567890abcdefgh",
        }
    )
    response = decide(request)

    assert response.decision == Decision.SANITIZE
    assert response.sanitized_payload is not None
    assert response.sanitized_payload["message"] == "My API key is [REDACTED]"
    assert "api_key" in response.sensitive_entities
    assert response.next_step == "sanitize"


def test_sanitize_never_executes_original_payload():
    router = _Router()
    request = _request(
        payload={
            "action": "send_message",
            "message": "My API key is sk-1234567890abcdefgh",
        }
    )
    response = decide(request)

    preview = asyncio.run(router.route(request, response))
    assert router.api.calls == []
    assert preview.status == ExecutionStatus.SANITIZED

    executed = asyncio.run(router.route(request, response, use_sanitized=True))
    assert len(router.api.calls) == 1
    executed_action = router.api.calls[0]
    assert executed_action.payload["message"] == "My API key is [REDACTED]"
    assert "sk-1234567890abcdefgh" not in executed_action.payload["message"]
    assert executed.status == ExecutionStatus.SUCCESS


def test_ask_user_never_calls_executor_and_explains_why():
    router = _Router()
    request = _request(risk_hint="ambiguous_target", target="john")
    response = decide(request)

    assert response.decision == Decision.ASK_USER
    assert any("clarification" in reason for reason in response.reasons)
    assert response.next_step == "ask_user"

    result = asyncio.run(router.route(request, response))
    assert router.browser.calls == []
    assert router.api.calls == []
    assert result.status == ExecutionStatus.WAITING_USER


def test_ask_user_updated_request_reruns_decision_and_executes():
    router = _Router()

    ambiguous = _request(risk_hint="ambiguous_target", target="john")
    first_response = decide(ambiguous)
    assert first_response.decision == Decision.ASK_USER
    asyncio.run(router.route(ambiguous, first_response))
    assert router.api.calls == []

    updated = _request(
        action_id="act_updated",
        risk_hint="unknown",
        target="john@company.com",
        payload={"action": "send_message", "to": "john@company.com"},
    )
    second_response = decide(updated)

    assert second_response.decision == Decision.ALLOW
    result = asyncio.run(router.route(updated, second_response))
    assert len(router.api.calls) == 1
    assert result.status == ExecutionStatus.SUCCESS


def test_decision_to_execution_status_covers_all_five():
    from app.executors.router import decision_to_execution_status

    assert decision_to_execution_status(Decision.BLOCK) == ExecutionStatus.BLOCKED
    assert decision_to_execution_status(Decision.NEED_APPROVAL) == ExecutionStatus.PENDING_APPROVAL
    assert decision_to_execution_status(Decision.SANITIZE) == ExecutionStatus.SANITIZED
    assert decision_to_execution_status(Decision.ASK_USER) == ExecutionStatus.WAITING_USER
    assert decision_to_execution_status(Decision.ALLOW) is None


@pytest.mark.parametrize(
    ("risk_hint", "expected_decision", "expected_status"),
    [
        ("destructive", Decision.BLOCK, ExecutionStatus.BLOCKED),
        ("external_send", Decision.NEED_APPROVAL, ExecutionStatus.PENDING_APPROVAL),
        ("ambiguous_target", Decision.ASK_USER, ExecutionStatus.WAITING_USER),
    ],
)
def test_run_guarded_action_short_circuits_non_allow(risk_hint, expected_decision, expected_status):
    from app.domains.agent.services import run_guarded_action
    from app.domains.audit.repositories import AuditRepository

    event = asyncio.run(
        run_guarded_action(
            {
                "action_type": "API_CALL",
                "target_system": "gmail",
                "target": "john@example.com",
                "risk_hint": risk_hint,
                "payload": {"action": "send_message"},
            },
            audit=AuditRepository(),
        )
    )

    assert event.decision_json["decision"] == expected_decision.value
    assert event.execution_status == expected_status
    # Non-ALLOW decisions must never reach an executor.
    assert event.execution_json["executor"] == "router"


def test_run_guarded_action_allow_executes_immediately(tmp_path, monkeypatch):
    """ALLOW must reach the executor right away. It does not guarantee
    success: an invalid gmail send still gets ALLOW but fails at execution,
    while a valid file read succeeds."""
    from app.config.settings import get_settings
    from app.domains.agent.services import run_guarded_action
    from app.domains.audit.repositories import AuditRepository

    # ALLOW + failing execution: decision is ALLOW, execution attempted and failed.
    failed = asyncio.run(
        run_guarded_action(
            {
                "action_type": "API_CALL",
                "target_system": "gmail",
                "target": "john@example.com",
                "risk_hint": "unknown",
                "payload": {"action": "send_message"},
            },
            audit=AuditRepository(),
        )
    )
    assert failed.decision_json["decision"] == Decision.ALLOW.value
    assert failed.execution_status == ExecutionStatus.FAILED

    # ALLOW + successful execution.
    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))
    get_settings.cache_clear()

    ok = asyncio.run(
        run_guarded_action(
            {
                "action_type": "FILE_READ",
                "target_system": "local_file",
                "target": "sample.txt",
                "risk_hint": "file_read",
                "payload": {"action": "read", "path": "sample.txt"},
            },
            audit=AuditRepository(),
        )
    )
    assert ok.decision_json["decision"] == Decision.ALLOW.value
    assert ok.execution_status == ExecutionStatus.SUCCESS
