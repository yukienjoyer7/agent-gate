import asyncio
from types import SimpleNamespace

import pytest

from app.core.run_schema import RunStatus
from app.domains.agent.services import run_service
from app.domains.agent.services.run_registry import RunRegistry


@pytest.mark.asyncio
async def test_start_agent_run_creates_background_task_with_metadata(monkeypatch) -> None:
    async def fake_loop(run):
        run.status = RunStatus.DONE

    registry = RunRegistry()
    monkeypatch.setattr(run_service, "run_agent_loop", fake_loop)

    run = run_service.start_agent_run(
        "hello",
        channel="telegram",
        channel_id="123",
        metadata={"update_id": 1},
        registry=registry,
    )
    await asyncio.wait_for(run.task, timeout=1)

    assert registry.get(run.run_id) is run
    assert run.status == RunStatus.DONE
    assert run.channel == "telegram"
    assert run.channel_id == "123"
    assert run.metadata == {"update_id": 1}


@pytest.mark.asyncio
async def test_run_with_timeout_marks_run_error_and_emits_event(monkeypatch) -> None:
    async def slow_loop(run):
        await asyncio.sleep(1)

    monkeypatch.setattr(run_service, "run_agent_loop", slow_loop)
    monkeypatch.setattr(
        run_service,
        "get_settings",
        lambda: SimpleNamespace(AGENT_RUN_TIMEOUT_SEC=0.01),
    )

    run = RunRegistry().create("slow")
    await run_service.run_with_timeout(run)

    assert run.status == RunStatus.ERROR
    event = await asyncio.wait_for(run.events.get(), timeout=1)
    assert event["type"] == "error"
    assert "timed out" in event["data"]["message"]
