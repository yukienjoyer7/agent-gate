from typing import Any

from app.core.schemas import ActionRequest, new_id


def build_action_request(proposal: dict[str, Any]) -> ActionRequest:
    payload = proposal.get("payload") or {}
    return ActionRequest(
        run_id=proposal.get("run_id") or new_id("run"),
        action_id=proposal.get("action_id") or new_id("act"),
        source=proposal.get("source", "cli"),
        domain=proposal.get("domain", "productivity"),
        action_type=proposal["action_type"],
        target_system=proposal["target_system"],
        target=proposal.get("target", proposal["target_system"]),
        content_context=proposal.get("content_context", ""),
        payload_summary=proposal.get("payload_summary", summarize_payload(payload)),
        payload=payload,
        risk_hint=proposal.get("risk_hint", "unknown"),
        rollback_available=proposal.get("rollback_available", False),
        confidence=proposal.get("confidence", 1.0),
    )


def summarize_payload(payload: dict[str, Any]) -> str:
    if not payload:
        return ""
    return ", ".join(sorted(payload.keys()))
