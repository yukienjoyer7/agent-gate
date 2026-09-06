from typing import Any

from app.config.settings import get_settings
from app.core.schemas import ActionRequest, new_id


def build_action_request(proposal: dict[str, Any]) -> ActionRequest:
    payload = proposal.get("payload") or {}
    target_system = proposal["target_system"]
    domain = proposal.get("domain") or get_settings().DEFAULT_DOMAIN
    risk_hint = proposal.get("risk_hint", "unknown")
    payload_summary = proposal.get("payload_summary", summarize_payload(payload))
    if target_system == "stripe":
        # Financial policy is authoritative at the ActionRequest boundary.
        # This prevents a direct API caller or planner from downgrading a
        # Stripe write by claiming a low-risk domain/hint.
        domain = get_settings().DOMAIN_BY_TARGET_SYSTEM.get("stripe", "booking")
        stripe_action = payload.get("action")
        if stripe_action in {"create_checkout_session", "expire_checkout_session"}:
            risk_hint = "payment"
        elif stripe_action == "create_refund":
            risk_hint = "refund"
        payload_summary = summarize_stripe_payload(payload)
    return ActionRequest(
        run_id=proposal.get("run_id") or new_id("run"),
        action_id=proposal.get("action_id") or new_id("act"),
        source=proposal.get("source", "cli"),
        domain=domain,
        action_type=proposal["action_type"],
        target_system=target_system,
        target=proposal.get("target", target_system),
        recipient_reference=proposal.get("recipient_reference"),
        resolved_recipient=proposal.get("resolved_recipient"),
        user_goal=proposal.get("user_goal", ""),
        content_context=proposal.get("content_context", ""),
        payload_summary=payload_summary,
        payload=payload,
        risk_hint=risk_hint,
        rollback_available=proposal.get("rollback_available", False),
        confidence=proposal.get("confidence", 1.0),
    )


def summarize_payload(payload: dict[str, Any]) -> str:
    if not payload:
        return ""
    return ", ".join(sorted(payload.keys()))


def summarize_stripe_payload(payload: dict[str, Any]) -> str:
    action = str(payload.get("action") or "unknown")
    if action == "create_checkout_session":
        catalog_key = str(payload.get("catalog_key") or "missing catalog key")
        quantity = payload.get("quantity", 1)
        return f"create checkout for {catalog_key}, quantity {quantity}"
    if action == "create_refund":
        payment_intent = str(payload.get("payment_intent_id") or "missing payment intent")
        amount = payload.get("amount")
        amount_text = "full amount" if amount is None else f"minor-unit amount {amount}"
        return f"refund {payment_intent}, {amount_text}"
    if action in {"retrieve_checkout_session", "expire_checkout_session"}:
        return f"{action} {payload.get('session_id') or 'missing session'}"
    if action == "retrieve_refund":
        return f"retrieve_refund {payload.get('refund_id') or 'missing refund'}"
    return f"Stripe action {action}"
