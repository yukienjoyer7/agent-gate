"""Dedicated LLM guardrail judge (tool-calling).

The deterministic rules in :mod:`app.domains.guardrail.decision.simple`
cannot reason about semantics (is this recipient internal? is this payload a
secret the user must type?). When :attr:`GUARDRAIL_LLM_ENABLED` is on, the
hybrid :func:`app.domains.guardrail.decision.adecide` calls this judge with a
**dedicated model** (``GUARDRAIL_MODEL``, falling back to ``LLM_MODEL``) and
forces structured output through a
``submit_guardrail_decision`` tool call — the model cannot free-form its
verdict, so the decision enum is always parseable.

Safety rule (enforced by the caller, not the model): a rule-based BLOCK can
never be downgraded by this judge.
"""

from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import Any

from app.config.settings import get_settings
from app.core.schemas import ActionRequest, Decision, DecisionResponse, RiskLevel
from app.domains.guardrail.sensitive import is_sensitive_key
from app.llm.services.client import extract_message, post_chat

logger = logging.getLogger(__name__)

GUARDRAIL_TOOL_NAME = "submit_guardrail_decision"

GUARDRAIL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": GUARDRAIL_TOOL_NAME,
        "description": "Submit the final guardrail verdict for an AI-proposed action.",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["ALLOW", "BLOCK", "NEED_APPROVAL"],
                    "description": (
                        "ALLOW = safe to execute; BLOCK = never execute; "
                        "NEED_APPROVAL = user must confirm first."
                    ),
                },
                "reasons": {"type": "array", "items": {"type": "string"}},
                "triggered_policies": {"type": "array", "items": {"type": "string"}},
                "sensitive_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Free-form names of sensitive data involved.",
                },
                "sanitized_payload": {
                    "type": "object",
                    "description": (
                        "Map of payload key -> short reason, for keys whose value "
                        "the USER must type (empty secrets, {{placeholder}} markers). "
                        "The loop pauses for input before executing."
                    ),
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["decision", "reasons"],
        },
    },
}

GUARDRAIL_SYSTEM_PROMPT = """You are AgentGate's security guardrail judge. Decide whether an AI-proposed \
action is safe to execute. Base your verdict on the exact payload keys and the user's goal.

Policies:
- BLOCK: destructive, unauthorized, or data-exfiltration actions, and anything that harms \
the user or third parties (deleting data, bypassing auth, stealing data, impersonation).
- NEED_APPROVAL: actions with financial impact, external sends, bulk operations, refunds, \
or irreversible side effects — the user should confirm first.
- ALLOW: routine, reversible, non-sensitive actions.
- If a payload field is a secret the user must provide (empty password/token/api_key, or a \
{{placeholder}} marker), list that key in sanitized_payload with a short reason and set the \
decision to ALLOW (execution pauses for user input).

Never repeat secret values. Never downgrade an obviously destructive action."""


async def decide_llm(action: ActionRequest) -> DecisionResponse:
    """Ask the dedicated guardrail model for a structured verdict.

    Raises on any failure (missing key, bad response) — the hybrid wrapper
    falls back to the deterministic rules.
    """
    settings = get_settings()
    if not settings.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY is not configured")

    model = settings.GUARDRAIL_MODEL or settings.LLM_MODEL
    started = perf_counter()

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": GUARDRAIL_SYSTEM_PROMPT},
            {"role": "user", "content": _describe_action(action)},
        ],
        "temperature": 0,
        "stream": False,
        "tools": [GUARDRAIL_TOOL],
    }

    data, _ = await post_chat(payload, fallback_system_prompt=GUARDRAIL_SYSTEM_PROMPT)
    arguments = _extract_decision(data)

    decision_raw = str(arguments.get("decision") or "").upper()
    try:
        decision = Decision(decision_raw)
    except ValueError as exc:
        raise ValueError(f"invalid guardrail decision: {decision_raw!r}") from exc
    if decision not in (Decision.ALLOW, Decision.BLOCK, Decision.NEED_APPROVAL):
        raise ValueError(f"unexpected guardrail decision: {decision_raw!r}")

    sanitized = arguments.get("sanitized_payload")
    sensitive = arguments.get("sensitive_entities")
    reasons = [str(reason) for reason in (arguments.get("reasons") or [])][:10]

    risk_score = {"BLOCK": 0.95, "NEED_APPROVAL": 0.65}.get(decision.value, 0.1)
    risk_level = (
        RiskLevel.CRITICAL
        if decision == Decision.BLOCK
        else RiskLevel.HIGH if decision == Decision.NEED_APPROVAL else RiskLevel.LOW
    )

    logger.info(
        "guardrail LLM verdict",
        extra={
            "llm_model": model,
            "action_id": action.action_id,
            "decision": decision.value,
            "reasons": reasons,
        },
    )

    return DecisionResponse(
        run_id=action.run_id,
        action_id=action.action_id,
        decision=decision,
        risk_level=risk_level,
        risk_score=risk_score,
        reasons=reasons,
        triggered_policies=[str(policy) for policy in (arguments.get("triggered_policies") or [])][
            :10
        ],
        sensitive_entities=(
            [str(entity) for entity in sensitive] if isinstance(sensitive, list) else []
        ),
        sanitized_payload=sanitized if isinstance(sanitized, dict) else None,
        next_step="approval_queue" if decision == Decision.NEED_APPROVAL else "execute",
        latency_ms=int((perf_counter() - started) * 1000),
    )


def _extract_decision(data: dict[str, Any]) -> dict[str, Any]:
    """Pull the structured decision from the model response.

    Prefers the ``submit_guardrail_decision`` tool call; falls back to plain
    JSON content (used when the router rejects the ``tools`` parameter and
    the request was retried without tools).
    """
    message = extract_message(data)
    tool_calls = message.get("tool_calls") or []
    for tool_call in tool_calls:
        function = tool_call.get("function") or {}
        if function.get("name") == GUARDRAIL_TOOL_NAME:
            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError as exc:
                raise ValueError("guardrail tool arguments are not valid JSON") from exc
            if isinstance(args, dict):
                return args

    content = message.get("content")
    if isinstance(content, str) and content.strip():
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed

    raise ValueError("guardrail model returned no decision")


def _describe_action(action: ActionRequest) -> str:
    """JSON description of the action for the judge (secrets masked)."""
    payload = dict(action.payload or {})
    for key, value in list(payload.items()):
        if isinstance(value, str) and len(value) > 200:
            payload[key] = value[:200] + "..."
        if is_sensitive_key(key) and value not in ("", None):
            payload[key] = "\u2022\u2022\u2022\u2022"

    return json.dumps(
        {
            "action_type": action.action_type,
            "target_system": action.target_system,
            "target": str(action.target)[:300],
            "domain": action.domain,
            "risk_hint": action.risk_hint,
            "payload_summary": action.payload_summary,
            "content_context": action.content_context[:500],
            "payload": payload,
        },
        ensure_ascii=False,
    )
