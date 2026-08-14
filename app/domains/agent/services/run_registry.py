"""In-memory registry of active agent runs (single-process MVP).

Bridges the interactive loop (``app.domains.agent.services.agent_loop``) and
the HTTP API (``app.api.v1.chat``):

- ``POST /chat/execute`` / ``/chat/execute/stream`` create a
  :class:`RunSession` and start the loop task.
- When a step needs approval or user input, the loop registers an
  ``asyncio`` waiter; ``POST /chat/execute/{run_id}/respond`` resolves it
  (or stores the response as "pending" if the waiter is not registered yet).
- The session's event queue is drained by the SSE generator.

Note: in-memory only — a multi-instance deployment would need a shared store.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config.settings import get_settings
from app.core.run_schema import RunStatus, StepStatus
from app.core.schemas import new_id
from app.domains.guardrail.sensitive import is_sensitive_key


@dataclass
class StepState:
    """One plan step inside an active run, with its live lifecycle state."""

    index: int
    data: dict[str, Any]
    action_id: str
    status: StepStatus = StepStatus.PENDING
    decision: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    sanitize_fields: list[dict[str, Any]] | None = None
    answered: set[str] = field(default_factory=set)
    audit_event: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        """Step as exposed over SSE — sensitive payload values masked."""
        data = dict(self.data)
        payload = data.get("payload")
        if isinstance(payload, dict):
            payload = {
                key: (
                    "\u2022\u2022\u2022\u2022"
                    if is_sensitive_key(key) and value not in ("", None)
                    else value
                )
                for key, value in payload.items()
            }
            data["payload"] = payload
        return {
            **data,
            "index": self.index,
            "action_id": self.action_id,
            "status": self.status.value,
            "decision": self.decision,
            "execution": self.execution,
        }


class RunSession:
    """Mutable state of one agent run, shared by the loop task and the API."""

    def __init__(self, prompt: str) -> None:
        self.run_id: str = new_id("run")
        self.prompt: str = prompt
        self.status: RunStatus = RunStatus.RUNNING
        self.steps: list[StepState] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.waiters: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.pending_responses: dict[int, dict[str, Any]] = {}
        self.execution_log: list[dict[str, Any]] = []
        self.last_observation: str = ""
        self.replan_count: int = 0
        self.task: asyncio.Task[Any] | None = None
        self.created_at: datetime = datetime.now(UTC)

    def step(self, index: int) -> StepState | None:
        return next((step for step in self.steps if step.index == index), None)

    def public_steps(self) -> list[dict[str, Any]]:
        return [step.public() for step in self.steps]


class RunRegistry:
    """Thread-safe in-memory registry keyed by ``run_id``."""

    def __init__(self) -> None:
        self._sessions: dict[str, RunSession] = {}
        self._lock = threading.Lock()

    def create(self, prompt: str) -> RunSession:
        session = RunSession(prompt)
        max_sessions = get_settings().RUN_REGISTRY_MAX_SESSIONS
        with self._lock:
            if len(self._sessions) >= max_sessions:
                oldest = min(self._sessions.values(), key=lambda run: run.created_at)
                self._sessions.pop(oldest.run_id, None)
            self._sessions[session.run_id] = session
        return session

    def get(self, run_id: str) -> RunSession | None:
        with self._lock:
            return self._sessions.get(run_id)

    def respond(
        self,
        run: RunSession,
        step_index: int,
        action: str,
        *,
        fields: dict[str, str] | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        """Validate and deliver a user response to a paused step.

        Raises ``LookupError`` when the step does not exist and ``ValueError``
        when the step is not waiting for this kind of response (HTTP 404/409
        upstream).
        """
        step = run.step(step_index)
        if step is None:
            raise LookupError("step not found")

        if action in ("approve", "decline"):
            if step.status != StepStatus.WAITING_APPROVAL:
                raise ValueError("step is not waiting for approval")
        elif action == "input":
            if step.status != StepStatus.WAITING_INPUT:
                raise ValueError("step is not waiting for input (sanitize)")
            if not fields and text is None:
                raise ValueError("input response requires 'fields' or 'text'")
        else:
            raise ValueError(f"unknown response action: {action!r}")

        payload = {"action": action, "fields": fields or {}, "text": text}

        waiter = run.waiters.pop(step_index, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(payload)
        else:
            # Loop has not registered the waiter yet — it will pick this up
            # right before pausing.
            run.pending_responses[step_index] = payload
        return payload


run_registry = RunRegistry()
