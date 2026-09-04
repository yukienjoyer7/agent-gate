"""Hybrid guardrail entry point: deterministic rules first, then an optional
dedicated LLM judge for non-BLOCK cases.

Design (as agreed in the conversation):
- Rule-based BLOCK is **final** and never overridden by the LLM.
- Every other rule verdict (ALLOW / NEED_APPROVAL) is sent to the dedicated
  guardrail model (``GUARDRAIL_MODEL``) for a second opinion when
  ``GUARDRAIL_LLM_ENABLED`` is true. The LLM may escalate to BLOCK, downgrade
  to ALLOW, or flag ``sanitized_payload``/``sensitive_entities`` — the agent
  loop turns those into a user-input ("sanitize") pause.
- Any LLM failure falls back to the rule verdict.
"""

from __future__ import annotations

import logging

from app.config.settings import get_settings
from app.core.schemas import ActionRequest, Decision, DecisionResponse
from app.domains.guardrail.decision.llm import decide_llm
from app.domains.guardrail.decision.simple import decide_rule

logger = logging.getLogger(__name__)


async def adecide(action: ActionRequest) -> DecisionResponse:
    """Async guardrail decision: rules, optionally refined by the LLM judge."""
    rule = decide_rule(action)
    settings = get_settings()
    if not settings.GUARDRAIL_LLM_ENABLED or rule.decision == Decision.BLOCK:
        return rule

    try:
        verdict = await decide_llm(action)
    except Exception as exc:  # noqa: BLE001 - rules are always the fallback
        logger.warning("LLM guardrail failed (%s); using rule decision", exc)
        return rule

    if verdict.decision in (Decision.ALLOW, Decision.BLOCK, Decision.NEED_APPROVAL):
        return verdict
    return rule
