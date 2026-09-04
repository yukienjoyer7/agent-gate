"""Run / step lifecycle enums for the reactive agent loop.

These describe the *interactive* lifecycle of a chat run (plan → guardrail →
approve / sanitize → execute → observe → replan), which is a layer above the
write-once :class:`app.core.action_schema.ExecutionStatus` used by the audit
log.

- ``WAITING_APPROVAL`` — a guardrail NEED_APPROVAL step is paused until the
  user approves or declines via ``POST /chat/execute/{run_id}/respond``.
- ``WAITING_INPUT`` — the "sanitize" state: the step needs a value the user
  must type (password, API key, or any ``{{placeholder}}`` payload value),
  so execution pauses until the respond endpoint supplies it.
"""

from __future__ import annotations

from enum import StrEnum


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    APPROVED = "approved"
    DECLINED = "declined"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    DECLINED = "declined"
    ERROR = "error"
    CANCELLED = "cancelled"
