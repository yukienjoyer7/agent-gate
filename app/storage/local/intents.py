"""Write-ahead intent journal. Unknown effects are never automatically replayed."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.schemas import ActionRequest, ExecutionResult, ExecutionStatus
from app.runtime.safety import Sanitizer
from app.storage.local.database import LocalDatabase, json_text
from app.storage.local.repositories import now


class UnknownOutcome(ValueError):
    pass


def is_write(request: ActionRequest) -> bool:
    if request.target_system == "stripe":
        return request.payload.get("action") in {
            "create_checkout_session",
            "create_refund",
            "expire_checkout_session",
        }
    if request.target_system == "calendar":
        return request.payload.get("action") == "create_event"
    return request.action_type in {
        "BROWSER_CLICK",
        "BROWSER_SUBMIT",
        "BROWSER_TYPE",
        "BROWSER_SELECT",
    }


class IntentJournal:
    def __init__(self, database: LocalDatabase, sanitizer: Sanitizer) -> None:
        self.database, self.sanitizer = database, sanitizer

    def begin(
        self, request: ActionRequest, operation_key: str | None = None
    ) -> tuple[str, ExecutionResult | None]:
        operation = str(request.payload.get("action") or request.action_type)
        material = {"action_type": request.action_type, "payload": request.payload}
        if request.target_system == "stripe" and is_write(request):
            fields = {
                "create_checkout_session": ("catalog_key", "quantity", "customer_email"),
                "create_refund": ("payment_intent_id", "amount", "reason"),
                "expire_checkout_session": ("session_id",),
            }[operation]
            normalized = {field: request.payload.get(field) for field in fields}
            if operation == "create_checkout_session":
                normalized["quantity"] = request.payload.get("quantity", 1)
            for field in ("catalog_key", "customer_email", "payment_intent_id", "session_id"):
                value = normalized.get(field)
                if isinstance(value, str):
                    normalized[field] = value.strip()
            material = {"operation": operation, "payload": normalized}
        else:
            material["target"] = request.target
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        key = operation_key or f"{request.run_id}:{request.action_id}:{operation}"
        if is_write(request):
            previous = (
                None
                if request.target_system == "browser"
                else self.database.db.execute(
                    "SELECT * FROM intents WHERE run_id=? AND target_system=? AND action=? "
                    "AND request_hash=? AND state='completed' LIMIT 1",
                    (request.run_id, request.target_system, operation, digest),
                ).fetchone()
            )
            if previous and previous["result_json"]:
                return key, ExecutionResult.model_validate_json(previous["result_json"]).model_copy(
                    update={"run_id": request.run_id, "action_id": request.action_id}
                )
            if self.database.db.execute(
                "SELECT 1 FROM intents WHERE target_system=? AND state IN ('started','unknown') "
                "AND action IN ('create_checkout_session','create_refund','expire_checkout_session',"
                "'create_event','BROWSER_CLICK','BROWSER_SUBMIT','BROWSER_TYPE','BROWSER_SELECT') LIMIT 1",
                (request.target_system,),
            ).fetchone():
                raise UnknownOutcome(
                    "A previous write has an unknown outcome. Reconcile it before another write; "
                    "Stripe status can be checked with 'agentgate payments sync'"
                )
        with self.database.db as db:
            db.execute(
                "INSERT INTO intents(operation_key,run_id,action_id,target_system,action,"
                "request_hash,state,created_at) VALUES(?,?,?,?,?,?,'started',?) "
                "ON CONFLICT(operation_key) DO UPDATE SET state='started' "
                "WHERE intents.state='failed'",
                (
                    key,
                    request.run_id,
                    request.action_id,
                    request.target_system,
                    operation,
                    digest,
                    now(),
                ),
            )
        return key, None

    def finish(self, key: str, request: ActionRequest, result: ExecutionResult) -> None:
        state = "completed" if result.status == ExecutionStatus.SUCCESS else "failed"
        error = result.error or {}
        if (
            is_write(request)
            and result.status == ExecutionStatus.FAILED
            and (
                not error
                or error.get("retryable")
                or error.get("code") in {"UNKNOWN", "UNAVAILABLE", "TIMEOUT"}
            )
        ):
            state = "unknown"
        remote_id = result.data.get("id") or error.get("details", {}).get("stripe_session_id")
        with self.database.db as db:
            db.execute(
                "UPDATE intents SET state=?,remote_id=?,result_json=? WHERE operation_key=?",
                (
                    state,
                    remote_id,
                    json_text(self.sanitizer.clean(result.model_dump(mode="json"))),
                    key,
                ),
            )

    def unknown(self, key: str) -> None:
        with self.database.db as db:
            db.execute("UPDATE intents SET state='unknown' WHERE operation_key=?", (key,))

    def financial_records(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.database.db.execute(
                "SELECT operation_key,action,remote_id,created_at,state FROM intents WHERE target_system='stripe'"
            )
        ]

    def reconciled(self, remote_id: str, status: str | None) -> None:
        with self.database.db as db:
            db.execute(
                "UPDATE intents SET state='completed' WHERE remote_id=? AND target_system='stripe' "
                "AND state='unknown' AND (action!='expire_checkout_session' OR ?='expired')",
                (remote_id, status),
            )
