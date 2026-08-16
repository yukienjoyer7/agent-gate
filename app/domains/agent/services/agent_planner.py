"""Reactive replanner: decides what to do *next* from the latest observation.

The initial plan is generated upfront by the LLM planner. When a step fails
(e.g. the page shows a login form instead of the expected element) or the
plan is exhausted (e.g. the calendar connector returned zero events), the
agent loop calls :func:`parse_next_steps` to branch: the model reads the
execution history + observation (page snapshot labels, connector result) and
returns the next step(s) — or ``done``.

Same model as the planner (``LLM_MODEL``); the guardrail, by design, uses its
own model (``GUARDRAIL_MODEL``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config.settings import get_settings
from app.llm.services.client import extract_message, post_chat
from app.llm.services.parser import _extract_json, _normalize_step

logger = logging.getLogger(__name__)


def _replan_system_prompt() -> str:
    """Replanner prompt; the schema value lists are injected from config
    (settings.ALLOWED_*) so env overrides reach the model automatically."""
    settings = get_settings()
    action_types = ", ".join(settings.ALLOWED_ACTION_TYPES)
    target_systems = ", ".join(settings.ALLOWED_TARGET_SYSTEMS)
    return f"""You are AgentGate's reactive replanner. Given the user's goal, the steps \
executed so far, and the latest observation, decide the single next step (or a small batch) that \
moves the goal forward. Return ONLY valid JSON with this shape:
{{
  "next_steps": [ <step> ] | null,
  "done": bool,
  "explanation": "one-line reason"
}}
Rules:
- <step> must follow the same schema as the initial plan: action_type ({action_types}), \
target_system ({target_systems}), target, domain, risk_hint, payload.
- Use the EXACT element role/label from the observation when emitting BROWSER_CLICK/TYPE steps.
- If the goal is achieved, impossible, or needs user clarification: done=true, next_steps=null.
- If a login is required, emit the login steps (fill username/password, submit) using the labels \
observed on the page. Those steps are NOT "external_send" — use risk_hint "unknown" for them.
- risk_hint "external_send" is ONLY for API_CALL / connector steps that transmit data to a
  third-party system. Typing/clicking on a page the user asked to open is not external_send.
- If a connector returned empty/missing data, propose a sensible fallback (different query, retry, \
or stop with done=true).
- Keep the batch minimal (1-3 steps). Never invent secrets — leave {{password}} style placeholders \
in the payload when the value must come from the user.
- Only emit BROWSER_SCREENSHOT when the user explicitly asked for a screenshot. Never add it for
  observation — use the element labels from the observation instead."""


async def parse_next_steps(prompt: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Ask the LLM for the next step(s). Returns an empty list for "done".

    Raises on any failure (missing key, bad JSON, unparseable steps) so the
    agent loop can surface the error instead of silently ending the run.
    """
    settings = get_settings()
    if not settings.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY is not configured")

    model = settings.LLM_MODEL
    user_content = json.dumps({"goal": prompt, **context}, ensure_ascii=False)

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _replan_system_prompt()},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "stream": False,
        "response_format": {"type": "json_object"},
    }

    data, _ = await post_chat(payload)
    message = extract_message(data)
    content = message.get("content")
    raw = _extract_json(content)

    if raw.get("done"):
        return []

    raw_steps = raw.get("next_steps")
    if not isinstance(raw_steps, list):
        # Single-step shape {"next_step": {...}} is also accepted.
        single = raw.get("next_step")
        raw_steps = [single] if isinstance(single, dict) else []

    steps: list[dict[str, Any]] = []
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            continue
        normalized = _normalize_step(raw_step)
        if normalized is not None:
            normalized["replanned"] = True
            steps.append(normalized)

    if not steps and not raw.get("done"):
        logger.warning(
            "replanner returned no usable steps",
            extra={"llm_model": model, "response": raw},
        )
    return steps
