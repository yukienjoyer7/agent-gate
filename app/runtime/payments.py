"""Explicit local payment status reconciliation, without webhook fulfillment."""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING, Any

from app.core.schemas import ExecutionStatus
from app.credentials.local import CredentialError

if TYPE_CHECKING:
    from app.runtime.local import LocalRuntime


async def sync_payments(runtime: LocalRuntime) -> dict[str, Any]:
    if not runtime.settings.STRIPE_SECRET_KEY.startswith(("sk_test_", "rk_test_")):
        raise CredentialError("Payments sync requires a Stripe test-mode credential")
    if importlib.util.find_spec("stripe") is None:
        raise ValueError(
            "Install the Stripe extra from your source checkout or release wheel before payments sync"
        )
    journal = runtime.router.intents
    tracked = runtime.payments.list()
    # An intent's remote ID survives a secondary payment-row persistence failure.
    for record in journal.financial_records():
        remote_id = record["remote_id"]
        if remote_id and not any(item["id"] == remote_id for item in tracked):
            tracked.append(
                {"id": remote_id, "kind": "refund" if "refund" in record["action"] else "checkout"}
            )
    failures = []
    connector = runtime.connectors["stripe"]
    for item in tracked:
        refund = item["kind"] == "refund"
        result = await connector.execute(
            "retrieve_refund" if refund else "retrieve_checkout_session",
            {
                "run_id": "sync",
                "action_id": "sync:" + item["id"],
                "refund_id" if refund else "session_id": item["id"],
            },
        )
        if result.status != ExecutionStatus.SUCCESS:
            failures.append({"id": item["id"], "error": result.result_summary})
            continue
        runtime.payments.record(item["kind"], result.data)
        journal.reconciled(item["id"], result.data.get("status"))
    unresolved = [
        record
        for record in journal.financial_records()
        if record["state"] in {"started", "unknown"}
    ]
    return {"payments": runtime.payments.list(), "failures": failures, "unresolved": unresolved}
