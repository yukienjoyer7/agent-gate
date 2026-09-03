"""Shared lifecycle service for reactive AgentGate runs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config.settings import get_settings
from app.core.run_schema import RunStatus
from app.domains.agent.services.agent_loop import run_agent_loop
from app.domains.agent.services.run_registry import RunRegistry, RunSession, run_registry

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.DONE,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
        RunStatus.DECLINED,
        RunStatus.ERROR,
        RunStatus.CANCELLED,
    }
)


def start_agent_run(
    prompt: str,
    *,
    channel: str | None = None,
    channel_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    registry: RunRegistry = run_registry,
) -> RunSession:
    """Create a run session and start the reactive agent loop in the background."""
    run = registry.create(prompt, channel=channel, channel_id=channel_id, metadata=metadata)
    run.task = asyncio.create_task(run_with_timeout(run))
    return run


async def run_with_timeout(run: RunSession) -> None:
    """Run the agent loop with the configured hard overall deadline."""
    timeout = get_settings().AGENT_RUN_TIMEOUT_SEC
    try:
        await asyncio.wait_for(run_agent_loop(run), timeout=timeout)
    except TimeoutError:
        logger.warning("run timed out after %ss: %s", timeout, run.run_id)
        run.status = RunStatus.ERROR
        run.events.put_nowait(
            {
                "type": "error",
                "data": {"run_id": run.run_id, "message": f"run timed out after {timeout}s"},
            }
        )
    except asyncio.CancelledError:
        run.status = RunStatus.CANCELLED
        raise


def is_terminal(run: RunSession) -> bool:
    return run.status in TERMINAL_RUN_STATUSES
