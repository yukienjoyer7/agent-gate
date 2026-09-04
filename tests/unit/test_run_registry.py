"""Tests for the in-memory run registry (waiters + respond delivery)."""

import asyncio

import pytest

from app.core.run_schema import StepStatus
from app.domains.agent.services.run_registry import RunRegistry, RunSession, StepState


def _session_with_step(status: StepStatus = StepStatus.WAITING_APPROVAL) -> RunSession:
    run = RunSession("test prompt")
    run.steps.append(
        StepState(index=0, data={"action_type": "API_CALL"}, action_id="act_1", status=status)
    )
    return run


def test_create_get() -> None:
    registry = RunRegistry()
    run = registry.create("hello")
    assert registry.get(run.run_id) is run
    assert registry.get("nope") is None


def test_respond_rejects_unknown_step() -> None:
    registry = RunRegistry()
    run = _session_with_step()
    with pytest.raises(LookupError):
        registry.respond(run, 99, "approve")


def test_respond_rejects_wrong_status() -> None:
    registry = RunRegistry()
    run = _session_with_step(status=StepStatus.PENDING)
    with pytest.raises(ValueError, match="not waiting"):
        registry.respond(run, 0, "approve")


def test_respond_input_requires_fields_or_text() -> None:
    registry = RunRegistry()
    run = _session_with_step(status=StepStatus.WAITING_INPUT)
    with pytest.raises(ValueError, match="fields.*text"):
        registry.respond(run, 0, "input")


def test_respond_rejects_unknown_action() -> None:
    registry = RunRegistry()
    run = _session_with_step()
    with pytest.raises(ValueError, match="unknown response action"):
        registry.respond(run, 0, "ignore")


def test_respond_stores_pending_before_waiter_registered() -> None:
    """Response arriving before the loop registers the waiter is queued."""
    registry = RunRegistry()
    run = _session_with_step()
    registry.respond(run, 0, "approve")
    assert run.pending_responses[0]["action"] == "approve"
    assert run.waiters == {}


@pytest.mark.asyncio
async def test_respond_resolves_registered_waiter() -> None:
    registry = RunRegistry()
    run = _session_with_step(status=StepStatus.WAITING_INPUT)
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    run.waiters[0] = future

    registry.respond(run, 0, "input", fields={"password": "hunter2"})
    result = await asyncio.wait_for(future, timeout=1)
    assert result["action"] == "input"
    assert result["fields"] == {"password": "hunter2"}
    assert run.waiters == {}
