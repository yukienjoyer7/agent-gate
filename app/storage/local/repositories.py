"""Local repositories preserve the core audit and connector contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.core.schemas import ActionRequest, AuditEvent, DecisionResponse, ExecutionResult
from app.runtime.safety import Sanitizer
from app.storage.local.database import LocalDatabase, json_text


def now() -> str:
    return datetime.now(UTC).isoformat()


class RunStore:
    def __init__(self, database: LocalDatabase, sanitizer: Sanitizer) -> None:
        self.database = database
        self.sanitizer = sanitizer

    def interrupt_abandoned(self) -> None:
        with self.database.db as db:
            db.execute(
                "UPDATE runs SET status='interrupted', finished_at=? "
                "WHERE status IN ('running', 'waiting_approval', 'waiting_input')",
                (now(),),
            )
            db.execute("UPDATE intents SET state='unknown' WHERE state='started'")

    def create(self, run: Any) -> None:
        with self.database.db as db:
            db.execute(
                "INSERT INTO runs(run_id,prompt,status,created_at) VALUES(?,?,?,?)",
                (
                    run.run_id,
                    self.sanitizer.text(run.prompt),
                    run.status.value,
                    run.created_at.isoformat(),
                ),
            )

    def publish(self, run: Any, event: dict[str, Any]) -> dict[str, Any]:
        with self.database.db as db:
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?", (run.run_id,)
            ).fetchone()[0]
            envelope = self.sanitizer.clean(
                {
                    **event,
                    "schema_version": 1,
                    "run_id": run.run_id,
                    "sequence": sequence,
                    "timestamp": now(),
                }
            )
            db.execute(
                "INSERT INTO events VALUES(?,?,?)", (run.run_id, sequence, json_text(envelope))
            )
            db.execute(
                "UPDATE runs SET status=?,steps_json=? WHERE run_id=?",
                (run.status.value, json_text(self.sanitizer.clean(run.public_steps())), run.run_id),
            )
        return envelope

    def finish(self, run: Any, status: str | None = None) -> None:
        with self.database.db as db:
            db.execute(
                "UPDATE runs SET status=?,finished_at=?,steps_json=? WHERE run_id=?",
                (
                    status or run.status.value,
                    now(),
                    json_text(self.sanitizer.clean(run.public_steps())),
                    run.run_id,
                ),
            )

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.database.db.execute(
                "SELECT run_id,prompt,status,created_at,finished_at FROM runs "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        ]

    def get(self, run_id: str) -> dict[str, Any] | None:
        row = self.database.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["steps"] = json.loads(result.pop("steps_json"))
        result["events"] = [
            json.loads(event[0])
            for event in self.database.db.execute(
                "SELECT event_json FROM events WHERE run_id=? ORDER BY sequence", (run_id,)
            )
        ]
        result["audit"] = [
            json.loads(event[0])
            for event in self.database.db.execute(
                "SELECT event_json FROM audit WHERE run_id=? ORDER BY rowid", (run_id,)
            )
        ]
        return result


class LocalAuditStore:
    def __init__(self, database: LocalDatabase, sanitizer: Sanitizer) -> None:
        self.database = database
        self.sanitizer = sanitizer

    async def write(
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
            latency=latency or {},
        )
        with self.database.db as db:
            db.execute(
                "INSERT INTO audit VALUES(?,?,?)",
                (
                    request.action_id,
                    request.run_id,
                    json_text(self.sanitizer.clean(event.model_dump(mode="json"))),
                ),
            )
        # The live core still needs the original observation to replan.
        return event


class LocalTraceWriter:
    def __init__(self, database: LocalDatabase, sanitizer: Sanitizer) -> None:
        self.database = database
        self.sanitizer = sanitizer

    def write(self, trace: Any) -> Any:
        with self.database.db as db:
            db.execute(
                "INSERT INTO traces VALUES(?,?,?)",
                (
                    trace.action_id,
                    trace.run_id,
                    json_text(self.sanitizer.clean(trace.model_dump(mode="json"))),
                ),
            )
        return trace


class LocalPaymentStore:
    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    def record(self, kind: str, data: dict[str, Any]) -> None:
        remote_id = data.get("id")
        if not remote_id:
            raise ValueError("Payment response has no remote ID")
        allowed = {
            "id",
            "status",
            "payment_status",
            "payment_intent",
            "amount_total",
            "amount",
            "currency",
            "expires_at",
            "livemode",
        }
        safe = {key: value for key, value in data.items() if key in allowed}
        with self.database.db as db:
            db.execute(
                "INSERT INTO payments VALUES(?,?,?,?) ON CONFLICT(remote_id) "
                "DO UPDATE SET data_json=excluded.data_json,checked_at=excluded.checked_at",
                (remote_id, kind, json_text(safe), now()),
            )

    async def record_checkout_session(self, session_data: dict[str, Any]) -> None:
        self.record("checkout", session_data)

    async def process_event(self, event: dict[str, Any]) -> bool:
        raise NotImplementedError("Local payments are reconciled with explicit sync, not webhooks")

    def list(self) -> list[dict[str, Any]]:
        return [
            {"kind": row["kind"], "checked_at": row["checked_at"], **json.loads(row["data_json"])}
            for row in self.database.db.execute("SELECT * FROM payments ORDER BY remote_id")
        ]
