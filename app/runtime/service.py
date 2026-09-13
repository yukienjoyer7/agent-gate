"""In-process application facade used by the terminal adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Protocol

from app.core.run_schema import RunStatus
from app.domains.agent.services.run_registry import RunRegistry, RunSession
from app.domains.agent.services.run_service import start_agent_run


class RunHistory(Protocol):
    def create(self, run: RunSession) -> None: ...
    def publish(self, run: RunSession, event: dict[str, Any]) -> dict[str, Any]: ...


class AgentRuntime(Protocol):
    @property
    def runs(self) -> RunHistory: ...


class AgentService:
    def __init__(self, runtime: AgentRuntime) -> None:
        self.runtime = runtime
        self.registry = RunRegistry()
        self.run: RunSession | None = None

    def submit(self, prompt: str) -> RunSession:
        if self.run is not None:
            raise ValueError("This foreground service already owns a run")
        run = start_agent_run(prompt, channel="cli", registry=self.registry)
        self.run = run
        self.runtime.runs.create(run)
        run.event_handler = lambda event: self.runtime.runs.publish(run, event)
        return run

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        run = self._run()
        if run.task is None:
            raise RuntimeError("Run task is missing")
        while not run.task.done() or not run.events.empty():
            incoming = asyncio.create_task(run.events.get())
            try:
                done, _ = await asyncio.wait(
                    {incoming, run.task}, return_when=asyncio.FIRST_COMPLETED
                )
                if incoming in done:
                    yield incoming.result()
            finally:
                if not incoming.done():
                    incoming.cancel()
                    await asyncio.gather(incoming, return_exceptions=True)
        await run.task

    def respond(
        self, step_index: int, action_id: str, action: str, fields: dict[str, str] | None = None
    ) -> None:
        run = self._run()
        step = run.step(step_index)
        if step is None or step.action_id != action_id:
            raise ValueError("Response does not match the pending action")
        self.registry.respond(run, step_index, action, fields=fields)

    async def cancel(self) -> None:
        run = self.run
        if run is not None and run.task is not None and not run.task.done():
            run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)
            run.status = RunStatus.CANCELLED

    def _run(self) -> RunSession:
        if self.run is None:
            raise ValueError("No run has been submitted")
        return self.run
