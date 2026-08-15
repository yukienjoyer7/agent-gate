"""
LLM-backed natural-language → action plan parser.

The original rule-based parser (``parse_old.py``) has been removed; the
planner now always goes through the configured LLM provider (see
``LLM_MODEL`` / ``LLM_TYPE`` in settings).

Behaviour contract
------------------
The public functions keep the **same return shapes** as before, so callers
(``app/api/v1/chat.py``, ``app/domains/agent/services/agent_loop.py``,
tests) work unchanged:

- ``parse_prompt(prompt)`` → ``{"parsed", "llm_provider", "raw_prompt", "human_readable"}``
- ``parse_prompt_plan(prompt)`` → ``{"plan", "llm_provider", "raw_prompt", "human_readable"}``

Errors (missing ``LLM_API_KEY``, unparseable model output) propagate to the
caller — the reactive agent loop surfaces them as a run ``error`` event.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config.settings import get_settings
from app.core.browser_schema import BrowserElement
from app.llm.services.client import post_chat
from app.llm.tools import TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger(__name__)

# Aliases free models commonly return instead of the canonical action_type.
# ``action`` → ``action_type`` mapping.
ACTION_ALIASES: dict[str, str] = {
    "navigate": "BROWSER_OPEN",
    "navigate_to": "BROWSER_OPEN",
    "go_to": "BROWSER_OPEN",
    "goto": "BROWSER_OPEN",
    "open": "BROWSER_OPEN",
    "visit": "BROWSER_OPEN",
    "click": "BROWSER_CLICK",
    "tap": "BROWSER_CLICK",
    "press": "BROWSER_CLICK",
    "type": "BROWSER_TYPE",
    "fill": "BROWSER_TYPE",
    "fill_in": "BROWSER_TYPE",
    "input": "BROWSER_TYPE",
    "enter": "BROWSER_TYPE",
    "scroll": "BROWSER_SCROLL",
    "screenshot": "BROWSER_SCREENSHOT",
    "capture": "BROWSER_SCREENSHOT",
    "submit": "BROWSER_SUBMIT",
    "send": "BROWSER_SUBMIT",
    "select": "BROWSER_SELECT",
    "choose": "BROWSER_SELECT",
    "api_call": "API_CALL",
    "read": "FILE_READ",
    "read_file": "FILE_READ",
}


# Actions that resolve page elements by label/role — exactly the ones the
# accessibility-tree tool exists to support. Derived from
# INTERACTIVE_BROWSER_ACTIONS minus the page-level actions (scroll/screenshot).
# Set only once per process; safe because it reads the same cached settings.
def _element_interaction_actions() -> set[str]:
    settings = get_settings()
    return set(settings.INTERACTIVE_BROWSER_ACTIONS) - {"BROWSER_SCROLL", "BROWSER_SCREENSHOT"}


# Deterministic prompt-level risk keywords (moved from the removed rule-based
# parser). Any match overrides the LLM's risk_hint so destructive /
# unauthorized / data-exfiltration prompts always reach the guardrail as BLOCK
# regardless of what the model emitted. Ordered longest-phrase-first would be
# nicer, but the current keywords are all single tokens and match by substring.
RISK_KEYWORDS: list[tuple[str, str]] = [
    ("delete", "destructive"),
    ("remove", "destructive"),
    ("destroy", "destructive"),
    ("erase", "destructive"),
    ("wipe", "destructive"),
    ("purge", "destructive"),
    ("drop", "destructive"),
    ("kill", "destructive"),
    ("terminate", "destructive"),
    ("shutdown", "destructive"),
    ("unauthorized", "unauthorized"),
    ("bypass", "unauthorized"),
    ("hack", "unauthorized"),
    ("exfiltrate", "data_exfiltration"),
    ("steal", "data_exfiltration"),
    ("leak", "data_exfiltration"),
]


def _classify_prompt_risk(prompt_lower: str) -> str | None:
    """
    Return the first risk_hint matched by prompt keywords, or None.

    This is checked **before** per-step normalization, so destructive actions
    are always caught regardless of domain.
    """
    for keyword, risk_hint in RISK_KEYWORDS:
        if keyword in prompt_lower:
            return risk_hint
    return None


def _rules() -> dict[str, Any]:
    """Planner validation rules, sourced from config (single source of truth;
    see ``app.config.settings`` — overridable via env)."""
    settings = get_settings()
    return {
        "action_types": set(settings.ALLOWED_ACTION_TYPES),
        "target_systems": set(settings.ALLOWED_TARGET_SYSTEMS),
        "domains": set(settings.ALLOWED_DOMAINS),
        "risk_hints": set(settings.ALLOWED_RISK_HINTS),
        "interactive_actions": set(settings.INTERACTIVE_BROWSER_ACTIONS),
        "domain_by_target_system": settings.DOMAIN_BY_TARGET_SYSTEM,
    }


def _base_system_prompt() -> str:
    """Base planner prompt; the allowed value lists are injected from config
    (settings.ALLOWED_*) so an env override reaches the model automatically.
    Keeps the settings list order (BROWSER_OPEN first, etc.)."""
    settings = get_settings()
    return f"""You are AgentGate's action planner. Convert the user's natural-language \
instruction into a JSON action plan.

Return ONLY valid JSON (no markdown fences, no commentary) with this exact shape:
{{
  "plan": [
    {{
      "source": "chat",
      "domain": "<domain>",
      "action_type": "<ACTION_TYPE>",
      "target_system": "<TARGET_SYSTEM>",
      "target": "<url or identifier>",
      "risk_hint": "<risk_hint>",
      "payload": {{ ... }}
    }}
  ],
  "summary": "<one-line human readable summary>"
}}

Rules:
- action_type must be one of: {', '.join(settings.ALLOWED_ACTION_TYPES)}.
- For browser interactions that navigate to a page, ALWAYS emit a first step BROWSER_OPEN with payload {{"url": "..."}} followed by the actual action step.
- target_system must be one of: {', '.join(settings.ALLOWED_TARGET_SYSTEMS)}.
- domain must be one of: {', '.join(settings.ALLOWED_DOMAINS)}.
- risk_hint must be one of: {', '.join(settings.ALLOWED_RISK_HINTS)}.
- payload for browser click/type: {{"url": "...", "action_type": "...", "element_id": "<slug>", "label": "<human label>", "role": "<aria role>", "value": "<text to type if any>"}}
- payload for gmail: {{"action": "send"|"read"|"archive", "to": "...", "body": "...", "query": "..."}}
- payload for github: {{"action": "repo_metadata", "owner": "...", "repo": "..."}}
- payload for local_file: {{"action": "read", "path": "..."}}
- target: full URL for browser, recipient address for gmail, owner/repo for github, file path for local_file.
- Always prepend https:// to bare domains.
- Keep the plan as short as the instruction requires (single API_CALL/FILE_READ step for connectors)."""


TOOLS_SYSTEM_PROMPT = """

Tools
-----
You have access to the following tool:
- get_accessibility_tree(url, wait_until?, timeout_ms?): Opens the URL in a headless
  browser and returns the page's accessibility tree: interactive elements with their
  exact ARIA role, accessible label, and DOM attributes (name, placeholder, aria-label,
  id, data-testid, text).

Element interaction rules
-------------------------
- ALWAYS call get_accessibility_tree on the target URL BEFORE emitting BROWSER_CLICK,
  BROWSER_TYPE, BROWSER_SCROLL, or BROWSER_SELECT steps.
- Use the EXACT role/label returned by the tool in payload.role / payload.label. Never
  guess, abbreviate, or invent labels — always use the exact value the tool returned.
- Never use the element_id from the tool output — it is only an internal index and is
  NOT stable at execution time. Rely on role and label only.
- If the tool returns an error or the page has no matching element, emit the plan with
  the closest available element, or just BROWSER_OPEN and explain the gap in summary.
- You may call the tool again after a BROWSER_OPEN step to inspect the loaded page.
  Keep tool calls minimal: one call per URL is usually enough."""


def _system_prompt() -> str:
    """Full system prompt; tools section only when function calling is enabled."""
    if get_settings().LLM_TOOLS_ENABLED:
        return _base_system_prompt() + TOOLS_SYSTEM_PROMPT
    return _base_system_prompt()


# ── Public API (async, LLM-backed) ──────────────────────────────


async def parse_prompt(prompt: str) -> dict[str, Any]:
    """
    Parse a natural-language instruction into ActionRequest fields via the LLM.

    Raises on any failure (missing ``LLM_API_KEY``, bad model output); the
    agent loop surfaces the error as a run ``error`` event.
    """
    steps = (await _llm_plan(prompt))["plan"]
    parsed = _primary_step(steps)
    return {
        "parsed": parsed,
        "llm_provider": _provider_name(),
        "raw_prompt": prompt,
        "human_readable": _describe_steps(steps),
    }


async def parse_prompt_plan(prompt: str) -> dict[str, Any]:
    """
    Parse a natural-language instruction into a **multi-step plan** via the LLM.

    Raises on any failure (missing ``LLM_API_KEY``, bad model output); the
    agent loop surfaces the error as a run ``error`` event.
    """
    steps = (await _llm_plan(prompt))["plan"]
    return {
        "plan": steps,
        "llm_provider": _provider_name(),
        "raw_prompt": prompt,
        "human_readable": _describe_steps(steps),
    }


# ── OpenRouter call ──────────────────────────────────────────────────


def _provider_name() -> str:
    """Return the configured LLM model id (for ``llm_provider``)."""
    return get_settings().LLM_MODEL


async def _llm_plan(prompt: str) -> dict[str, Any]:
    """
    Call the configured LLM provider and return a normalized plan dict.

    Uses function calling: when the model requests ``get_accessibility_tree``,
    the tool is executed and its result is fed back into the conversation so
    the final plan can reference exact element labels/roles from the live page
    instead of guessed ones.
    """
    settings = get_settings()
    api_key = settings.LLM_API_KEY
    if not api_key:
        raise RuntimeError("LLM_API_KEY is not configured")

    model = settings.LLM_MODEL

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": prompt},
    ]

    max_tool_iterations = settings.LLM_MAX_TOOL_ITERATIONS
    tools_used = False
    for _ in range(max_tool_iterations + 1):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
            "plugins": [{"id": "response-healing"}],
        }
        if settings.LLM_TOOLS_ENABLED:
            payload["tools"] = TOOL_DEFINITIONS
        else:
            # JSON mode biases models to emit JSON content instead of calling
            # tools, so only request it when no tools are being offered.
            payload["response_format"] = {"type": "json_object"}

        data, tools_rejected = await _post_chat(payload)
        message = _extract_message(data)

        tool_calls = message.get("tool_calls")
        if tool_calls:
            tools_used = True
            logger.info(
                "OpenRouter tool call",
                extra={"llm_model": model, "tool_calls": tool_calls},
            )
            # Feed the assistant's tool-call message + results back so the
            # model can ground its plan in the real page elements.
            messages.append(message)
            for tool_call in tool_calls:
                messages.append(await _build_tool_message(tool_call))
            continue

        content = message.get("content")
        try:
            raw = _extract_json(content)
        except json.JSONDecodeError as exc:
            # Keep visibility of what the model actually returned when it is
            # not valid JSON (the INFO log below is only emitted on success).
            logger.warning(
                "OpenRouter returned non-JSON content",
                extra={"llm_model": model, "llm_raw": content[:2000]},
            )
            raise ValueError(f"LLM returned non-JSON content: {exc}") from exc

        steps = [_normalize_step(step) for step in raw.get("plan", []) if isinstance(step, dict)]
        steps = [step for step in steps if step is not None]
        # Prepended BROWSER_OPEN first, then apply prompt risk to ALL final steps
        # so the navigation step also carries a BLOCK hint for destructive prompts.
        steps = _ensure_open_step(steps)
        steps = _apply_prompt_risk(steps, prompt)

        if not steps:
            raise ValueError("LLM returned an empty plan")

        # Warn when a plan targets page elements but the model never used the
        # accessibility-tree tool — that usually means guessed labels. Skip when
        # tools are disabled or the router rejected them: the model was never
        # able to call the tool in those cases.
        if (
            settings.LLM_TOOLS_ENABLED
            and not tools_used
            and not tools_rejected
            and any(step.get("action_type") in _element_interaction_actions() for step in steps)
        ):
            logger.warning(
                "Browser element plan emitted without a tool call (labels may be guessed)",
                extra={"llm_model": model, "plan": steps},
            )

        # INFO level so the LLM output is visible in `docker compose up` logs.
        # Single structured log line: raw model response + the normalized plan
        # (extra=... makes python-json-logger emit nested JSON, not escaped text).
        logger.info(
            "OpenRouter LLM response",
            extra={"llm_model": model, "llm_response": raw, "plan": steps},
        )

        return {"plan": steps, "summary": raw.get("summary", "")}

    raise ValueError(f"LLM exceeded max tool iterations ({max_tool_iterations})")


async def _post_chat(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """POST to the configured LLM provider (delegates to the shared client).

    For the openai type, retries once without ``tools`` if the router rejects
    them (400), passing the tool-less base prompt so the model is not told to
    call a tool that no longer exists. Returns ``(data, tools_rejected)`` —
    see :func:`app.llm.services.client.post_chat`.
    """
    return await post_chat(payload, fallback_system_prompt=_base_system_prompt())


def _extract_message(data: dict[str, Any]) -> dict[str, Any]:
    """Return the assistant message dict from an OpenRouter response."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unexpected OpenRouter response shape: {exc}") from exc
    if not isinstance(message, dict):
        raise ValueError("unexpected OpenRouter message shape")
    return message


async def _build_tool_message(tool_call: dict[str, Any]) -> dict[str, Any]:
    """Execute one tool call and build the ``tool`` message to append."""
    call_id = str(tool_call.get("id") or "")
    function = tool_call.get("function") or {}
    name = str(function.get("name") or "")
    raw_args = function.get("arguments") or "{}"
    try:
        arguments = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}

    result = await execute_tool(name, arguments)
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(result),
    }


def _extract_json(content: str | None) -> dict[str, Any]:
    """Extract a JSON object from model output (handles markdown fences and
    the bare-array shape some free models return instead of {"plan": [...]})."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    # Free models sometimes return just the step list: [{...}, {...}] instead
    # of the documented {"plan": [...], "summary": ...} envelope.
    if text.startswith("[") and text.endswith("]"):
        steps = json.loads(text)
        if isinstance(steps, list):
            return {"plan": steps}

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


# ── Normalization & helpers ─────────────────────────────────────────


def _normalize_step(step: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce an LLM step into the canonical step schema (or None if unusable)."""
    rules = _rules()
    action_type = str(step.get("action_type") or "").upper()
    if action_type not in rules["action_types"]:
        # Free models sometimes use ``action``/lowercase aliases instead of
        # the canonical action_type. Map them before rejecting the step.
        action_type = ACTION_ALIASES.get(
            str(step.get("action") or "").lower()
        ) or ACTION_ALIASES.get(action_type.lower())
    target_system = str(step.get("target_system") or "browser").lower()

    if action_type not in rules["action_types"]:
        return None

    payload = step.get("payload")
    payload = payload if isinstance(payload, dict) else {}

    # Derive the domain from target_system when the LLM omits it, so guardrail
    # risk levels stay correct (mirrors settings.DOMAIN_BY_TARGET_SYSTEM).
    domain = str(
        step.get("domain") or rules["domain_by_target_system"].get(target_system, "browser")
    ).lower()

    risk_hint = str(step.get("risk_hint") or "").lower()
    if risk_hint not in rules["risk_hints"]:
        risk_hint = _derive_risk_hint(action_type, target_system, payload)

    target = (
        step.get("target")
        or step.get("url")  # models often return {action: navigate, url: ...}
        or payload.get("url")
        or payload.get("path")
        or ""
    )
    if (
        isinstance(target, str)
        and target_system == "browser"
        and target
        and not re.match(r"^https?://", target)
    ):
        target = f"https://{target}"
        if payload.get("url"):
            payload["url"] = target

    normalized: dict[str, Any] = {
        "source": "chat",
        "domain": domain if domain in rules["domains"] else "browser",
        "action_type": action_type,
        "target_system": target_system if target_system in rules["target_systems"] else "browser",
        "target": target,
        "risk_hint": risk_hint,
        "payload": payload,
    }

    # Mirror the rule-based parser: attach a BrowserElement when we have a label.
    label = payload.get("label")
    if action_type in rules["interactive_actions"] and label:
        try:
            element = BrowserElement(
                snapshot_id="",
                element_id="",
                role=str(payload.get("role") or ""),
                label=str(label),
                risk_hint=risk_hint,
            )
            normalized["browser_element"] = element.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - best-effort metadata enrichment
            logger.debug("could not attach browser_element: %s", exc)

    return normalized


def _derive_risk_hint(action_type: str, target_system: str, payload: dict[str, Any]) -> str:
    """Best-effort risk hint derivation when the LLM omits risk_hint."""
    if action_type == "FILE_READ" or target_system == "local_file":
        return "file_read"
    if target_system == "gmail" and payload.get("action") == "send":
        return "external_send"
    # Form submits with a label are treated as external_send (NEED_APPROVAL).
    if action_type in ("BROWSER_SUBMIT", "BROWSER_SELECT") and payload.get("label"):
        return "external_send"
    return "unknown"


def _ensure_open_step(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prepend a BROWSER_OPEN step if a browser action has a URL but no OPEN step."""
    if not steps or steps[0].get("action_type") == "BROWSER_OPEN":
        return steps

    first = steps[0]
    if first.get("target_system") == "browser" and first.get("target"):
        url = first["target"]
        return [
            {
                "source": "chat",
                "domain": first.get("domain", "browser"),
                "action_type": "BROWSER_OPEN",
                "target_system": "browser",
                "target": url,
                # inherit the action step's risk so destructive prompts stay
                # consistent even if pipeline ordering changes later
                "risk_hint": first.get("risk_hint", "unknown"),
                "payload": {"url": url},
            },
            *steps,
        ]
    return steps


def _apply_prompt_risk(steps: list[dict[str, Any]], prompt: str) -> list[dict[str, Any]]:
    """
    Deterministic safety net: re-run the prompt-level risk keyword detection
    (:data:`RISK_KEYWORDS`) on the raw prompt and override ``risk_hint`` on
    every step, so destructive/unauthorized/data_exfiltration keywords always
    set a BLOCK hint regardless of what the LLM emitted.
    """
    prompt_risk = _classify_prompt_risk(prompt.lower().strip())
    if not prompt_risk:
        return steps
    for step in steps:
        step["risk_hint"] = prompt_risk
    return steps


def _primary_step(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the last non-BROWSER_OPEN step (the actual action)."""
    for step in reversed(steps):
        if step.get("action_type") != "BROWSER_OPEN":
            return step
    return steps[-1] if steps else {}


def _describe_steps(steps: list[dict[str, Any]]) -> str:
    """Build a human-readable numbered description of the plan steps."""
    lines: list[str] = []
    for index, step in enumerate(steps, 1):
        target = step.get("target") or ""
        label = (step.get("payload") or {}).get("label") or ""
        action = step.get("action_type", "").replace("BROWSER_", "").title()
        description = (
            action + (f' "{label}"' if label else "") + (f" on {target}" if target else "")
        )
        lines.append(f"  {index}. {description}")
    return "\n" + "\n".join(lines)
