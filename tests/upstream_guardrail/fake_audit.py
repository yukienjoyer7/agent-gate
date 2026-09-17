"""In-memory audit store standing in for Postgres in unit tests.

The real store (agentgate/audit.py) is mandatory and connects eagerly, so the suite
substitutes this one to stay offline. It implements the same surface the engine and
loop call, and keeps the records so tests can assert on what was audited.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import patch

from app.domains.guardrail._vendor.agentgate.audit import STAGE_ACTION
from app.domains.guardrail._vendor.agentgate.schemas import ActionRequest, DecisionResponse


class FakeAuditStore:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def record(
        self,
        req: ActionRequest,
        decision: DecisionResponse,
        stage: str = STAGE_ACTION,
    ) -> str:
        audit_id = "aud_" + uuid.uuid4().hex[:12]
        decision.audit_id = audit_id
        self.records[audit_id] = {
            "audit_id": audit_id,
            "timestamp": time.time(),
            "stage": stage,
            "request": req.to_dict(),
            "response": decision.to_dict(),
            "execution_status": (
                "awaiting_approval" if decision.decision.value == "NEED_APPROVAL" else "pending"
            ),
            "reviewer_status": "none",
            "execution_result": None,
        }
        return audit_id

    def update(
        self,
        audit_id: str,
        *,
        execution_status: str | None = None,
        reviewer_status: str | None = None,
        execution_result: dict[str, Any] | None = None,
    ) -> None:
        record = self.records.get(audit_id)
        if record is None:
            return
        if execution_status is not None:
            record["execution_status"] = execution_status
        if reviewer_status is not None:
            record["reviewer_status"] = reviewer_status
        if execution_result is not None:
            record["execution_result"] = execution_result

    def get(self, audit_id: str) -> dict[str, Any] | None:
        return self.records.get(audit_id)

    def actions(self) -> list[dict[str, Any]]:
        """Proposed tool calls only, excluding the loop's internal screens."""
        return [r for r in self.records.values() if r["stage"] == STAGE_ACTION]

    def completeness(self) -> float:
        actions = self.actions()
        if not actions:
            return 1.0
        complete = sum(
            1
            for r in actions
            if r["request"] and r["response"] and r["execution_status"] and r["timestamp"]
        )
        return round(complete / len(actions), 4)

    def close(self) -> None:
        pass


def audit_patch():
    """Patcher that makes every default DecisionEngine use an in-memory store."""
    return patch(
        "app.domains.guardrail._vendor.agentgate.decision.build_audit_store", FakeAuditStore
    )
