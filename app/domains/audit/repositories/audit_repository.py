from __future__ import annotations

import json
from pathlib import Path

from app.config.settings import get_settings
from app.core.schemas import ActionRequest, AuditEvent, DecisionResponse, ExecutionResult


class AuditRepository:
    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or get_settings().AUDIT_LOG_PATH)

    def write(
        self,
        request: ActionRequest,
        decision: DecisionResponse,
        execution: ExecutionResult,
        latency: dict[str, int] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            run_id=request.run_id,
            action_id=request.action_id,
            request_json=request.model_dump(mode="json", exclude={"payload"}),
            decision_json=decision.model_dump(mode="json"),
            execution_json=execution.model_dump(mode="json"),
            execution_status=execution.status,
            error_type=(execution.error or {}).get("code"),
            latency=latency
            or {"guardrail_ms": decision.latency_ms, "executor_ms": execution.latency_ms},
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json")) + "\n")
        return event

    def latest(self) -> AuditEvent | None:
        if not self.path.exists():
            return None
        lines = [line for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        if not lines:
            return None
        return AuditEvent.model_validate_json(lines[-1])

    def list(self) -> list[AuditEvent]:
        if not self.path.exists():
            return []
        return [
            AuditEvent.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def by_run(self, run_id: str) -> list[AuditEvent]:
        return [event for event in self.list() if event.run_id == run_id]

    def by_action(self, action_id: str) -> AuditEvent | None:
        return next((event for event in self.list() if event.action_id == action_id), None)
