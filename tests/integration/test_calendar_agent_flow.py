"""Calendar create-event approval regression tests with no live Google API calls."""

import asyncio

import pytest

from app.core.run_schema import RunStatus, StepStatus
from app.core.schemas import AuditEvent, ExecutionResult, ExecutionStatus
from app.domains.agent.services import agent_loop, guarded_execution
from app.domains.agent.services.run_registry import run_registry
from app.domains.connector.calendar import CalendarConnector


class _RecordingAuditRepository:
    def __init__(self) -> None:
        self.writes: list[tuple] = []

    async def write(self, request, decision, execution, latency=None):
        self.writes.append((request, decision, execution))
        return AuditEvent(
            run_id=request.run_id,
            action_id=request.action_id,
            request_json=request.model_dump(mode="json", exclude={"payload"}),
            decision_json=decision.model_dump(mode="json"),
            execution_json=execution.model_dump(mode="json"),
            execution_status=execution.status,
        )


class _RecordingTraceWriter:
    def write(self, trace) -> None:
        return None


def _calendar_step() -> dict:
    return {
        "action_type": "API_CALL",
        "target_system": "calendar",
        "target": "primary",
        "domain": "productivity",
        "risk_hint": "external_send",
        "payload": {
            "action": "create_event",
            "summary": "meeting laplace #2",
            "start": "2026-09-13T18:00:00+07:00",
            "end": "2026-09-13T19:00:00+07:00",
        },
    }


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.01)


def _install_calendar_flow_fakes(monkeypatch):
    connector_calls: list[tuple[str, dict]] = []
    audit = _RecordingAuditRepository()

    async def fake_plan(prompt):
        return {"plan": [_calendar_step()], "llm_provider": "fake", "raw_prompt": prompt}

    async def fake_calendar_execute(self, action, payload):
        connector_calls.append((action, payload))
        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="calendar",
            status=ExecutionStatus.SUCCESS,
            result_summary="Created Calendar event: meeting laplace #2",
            data={"event": {"id": "evt_123", "summary": "meeting laplace #2"}},
        )

    async def fake_replan(prompt, context):
        return []

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "parse_next_steps", fake_replan)
    monkeypatch.setattr(CalendarConnector, "execute", fake_calendar_execute)
    monkeypatch.setattr(guarded_execution, "get_audit_repository", lambda: audit)
    monkeypatch.setattr(agent_loop, "get_audit_repository", lambda: audit)
    monkeypatch.setattr(guarded_execution, "TraceWriter", _RecordingTraceWriter)
    return connector_calls, audit


@pytest.mark.asyncio
async def test_calendar_create_event_waits_for_approval_then_executes_and_audits(monkeypatch):
    connector_calls, audit = _install_calendar_flow_fakes(monkeypatch)
    prompt = (
        '[HANYA GUNAKAN API_CALL] tambah event "meeting laplace #2" pada tanggal '
        "13 September 2026, mulai jam 18.00 sampai jam 19.00"
    )
    run = run_registry.create(prompt)
    task = asyncio.create_task(agent_loop.run_agent_loop(run))

    await _wait_for(lambda: run.status == RunStatus.WAITING_APPROVAL)
    assert run.steps[0].data["payload"] == _calendar_step()["payload"]
    assert run.steps[0].status == StepStatus.WAITING_APPROVAL
    assert connector_calls == []

    run_registry.respond(run, 0, "approve")
    await asyncio.wait_for(task, timeout=5)

    assert run.status == RunStatus.DONE
    assert run.steps[0].status == StepStatus.DONE
    assert connector_calls == [
        (
            "create_event",
            {
                "run_id": run.run_id,
                "action_id": run.steps[0].action_id,
                "target": "primary",
                **_calendar_step()["payload"],
            },
        )
    ]
    assert audit.writes[0][1].initial_decision.value == "NEED_APPROVAL"
    assert audit.writes[0][1].approval_decision == "approved"
    assert audit.writes[0][2].status == ExecutionStatus.SUCCESS


@pytest.mark.asyncio
async def test_calendar_create_event_rejection_never_calls_connector(monkeypatch):
    connector_calls, audit = _install_calendar_flow_fakes(monkeypatch)
    run = run_registry.create("create a calendar event")
    task = asyncio.create_task(agent_loop.run_agent_loop(run))

    await _wait_for(lambda: run.status == RunStatus.WAITING_APPROVAL)
    run_registry.respond(run, 0, "decline")
    await asyncio.wait_for(task, timeout=5)

    assert run.status == RunStatus.DECLINED
    assert run.steps[0].status == StepStatus.DECLINED
    assert connector_calls == []
    assert audit.writes[0][2].status == ExecutionStatus.SKIPPED


@pytest.mark.asyncio
async def test_calendar_missing_times_pause_for_input_then_still_require_approval(monkeypatch):
    connector_calls, _ = _install_calendar_flow_fakes(monkeypatch)

    async def incomplete_calendar_plan(prompt):
        step = _calendar_step()
        step["payload"] = {"action": "create_event", "summary": "meeting laplace #2"}
        return {"plan": [step], "llm_provider": "fake", "raw_prompt": prompt}

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", incomplete_calendar_plan)
    run = run_registry.create("create a calendar event on 13 September 2026")
    task = asyncio.create_task(agent_loop.run_agent_loop(run))

    await _wait_for(lambda: run.status == RunStatus.WAITING_INPUT)
    assert run.steps[0].status == StepStatus.WAITING_INPUT
    assert connector_calls == []

    run_registry.respond(
        run,
        0,
        "input",
        fields={
            "start": "2026-09-13T18:00:00+07:00",
            "end": "2026-09-13T19:00:00+07:00",
        },
    )
    await _wait_for(lambda: run.status == RunStatus.WAITING_APPROVAL)
    assert connector_calls == []

    run_registry.respond(run, 0, "approve")
    await asyncio.wait_for(task, timeout=5)

    assert run.status == RunStatus.DONE
    assert run.steps[0].data["payload"] == _calendar_step()["payload"]
    assert len(connector_calls) == 1
