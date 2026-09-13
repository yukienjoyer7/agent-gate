"""Lightweight browser entry points; Playwright is loaded only on execution."""

from typing import Any


def plan_step_to_browser_action(step: dict[str, Any]) -> dict[str, Any] | None:
    browser_type = {
        "BROWSER_CLICK": "click",
        "BROWSER_TYPE": "fill",
        "BROWSER_SCROLL": "scroll",
        "BROWSER_SCREENSHOT": "screenshot",
        "BROWSER_SUBMIT": "submit",
        "BROWSER_SELECT": "select",
    }.get(str(step.get("action_type") or ""))
    if browser_type is None:
        return None
    payload = step.get("payload") or {}
    payload = payload if isinstance(payload, dict) else {}
    action: dict[str, Any] = {"type": browser_type}
    for key in ("label", "element_id", "role"):
        if payload.get(key):
            action[key] = payload[key]
    value = payload.get("value") or payload.get("query") or payload.get("text")
    if value:
        action["value"] = value
    for key in ("delay_ms", "duration_ms", "x", "y", "path", "full_page"):
        if key in payload:
            action[key] = payload[key]
        elif key in step:
            action[key] = step[key]
    return action


def public_browser_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public = []
    for action in actions:
        item = dict(action)
        if item.get("type") in {"fill", "type"} and "value" in item:
            item["value"] = "••••"
        public.append(item)
    return public


async def run_browser_prototype_agent(**kwargs: Any):
    _require_browser()
    from app.domains.agent.services.browser_prototype_agent import run_browser_prototype_agent

    return await run_browser_prototype_agent(**kwargs)


async def run_browser_prototype_agent_atomic(**kwargs: Any):
    _require_browser()
    from app.domains.agent.services.browser_prototype_agent import (
        run_browser_prototype_agent_atomic,
    )

    return await run_browser_prototype_agent_atomic(**kwargs)


def _require_browser() -> None:
    from app.runtime.context import current_runtime

    runtime = current_runtime()
    if runtime is not None and not runtime.settings.LLM_TOOLS_ENABLED:
        raise ValueError("Browser capability is disabled. Run 'agentgate setup browser' first")
