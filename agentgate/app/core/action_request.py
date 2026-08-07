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


def deep_merge_payload(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """
    Merge `updates` into `base`, recursing into nested dicts instead of
    overwriting them wholesale. Used to fold a clarification response's
    payload_updates into the original proposal's payload.

    A plain `{**base, **updates}` shallow merge would replace an entire
    nested dict even if the update only meant to patch one key inside it
    -- e.g. base={"recipient": {"email": "a@x.com", "name": "A"}},
    updates={"recipient": {"email": "b@x.com"}} would shallow-merge to
    {"recipient": {"email": "b@x.com"}}, silently dropping "name". Deep
    merge instead recurses so siblings are preserved: {"recipient":
    {"email": "b@x.com", "name": "A"}}.

    Only dict values are merged recursively; lists and scalars in
    `updates` always replace the corresponding value in `base` (list
    concatenation/dedup would be ambiguous without knowing the field's
    semantics, so replacement is the safer default there).
    """
    merged = dict(base)
    for key, value in updates.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = deep_merge_payload(existing, value)
        else:
            merged[key] = value
    return merged
