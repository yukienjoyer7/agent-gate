from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.llm.services import parse_prompt_plan

router = APIRouter(prefix="/chat", tags=["chat"])


class ParseRequest(BaseModel):
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        examples=[
            "Click the login button on playwright.dev",
            "Send email to john@example.com saying hello",
            "Get repo info for microsoft/vscode",
            "Read file sample.txt",
        ],
        description="Natural language instruction",
    )


class ParseResponse(BaseModel):
    plan: list[dict[str, Any]] = Field(
        description=(
            "Ordered list of atomic steps. The AI automatically "
            "determines the domain and how many steps are needed.\n"
            '- Browser: "Open youtube.com" → 1 step\n'
            '- Browser: "Click ... on playwright.dev" → 2 steps (OPEN + CLICK)\n'
            '- Connector: "Send email ..." → 1 step (API_CALL)\n'
            '- Connector: "Get repo info ..." → 1 step (API_CALL)'
        ),
    )
    summary: str = Field(description="Human-readable plan summary")
    steps: int = Field(description="Number of steps in the plan")
    target: str = Field(description="Primary target URL or identifier")
    action_type: str = Field(description="Primary action type")
    llm_provider: str = Field(description="LLM provider used for planning")
    raw_prompt: str = Field(description="Original prompt text")


@router.post("/parse", response_model=ParseResponse)
async def parse_browser_action(request: ParseRequest) -> ParseResponse:
    """
    Parse a natural-language instruction into an **AI-generated plan**.

    The AI automatically analyses the prompt, detects the **domain**
    (browser, Gmail, GitHub, or file), and returns the optimal plan:

    **Browser** — ``"Click the login button on playwright.dev"``
      → 2 steps: ``BROWSER_OPEN`` then ``BROWSER_CLICK``

    **Gmail** — ``"Send email to user@example.com saying hello"``
      → 1 step: ``API_CALL`` (routed through gmail connector)

    **GitHub** — ``"Get repo info for owner/repo"``
      → 1 step: ``API_CALL`` (routed through github connector)

    **File** — ``"Read file sample.txt"``
      → 1 step: ``FILE_READ`` (routed through local_file connector)

    Each step in ``plan`` is compatible with
    :func:`app.core.action_request.build_action_request`.

    To **execute**, send the same prompt to ``POST /api/v1/chat/execute``.
    """
    result = parse_prompt_plan(request.prompt)
    plan = result["plan"]
    first = plan[0] if plan else {}
    payload = first.get("payload", {})

    return ParseResponse(
        plan=plan,
        summary=result.get("human_readable", "").strip(),
        steps=len(plan),
        target=first.get("target", "") or payload.get("url", "") or payload.get("path", ""),
        action_type=first.get("action_type", ""),
        llm_provider=result["llm_provider"],
        raw_prompt=result["raw_prompt"],
    )


@router.post("/execute")
async def execute_browser_plan(request: ParseRequest) -> dict[str, Any]:
    """
    Parse a natural-language instruction into an AI-generated plan and
    **execute** it through the appropriate pipeline:

    - **Browser** actions → Playwright (``run_browser_prototype_agent``)
    - **Connector** actions (Gmail, GitHub, file) → Guarded execution
      pipeline (``run_guarded_action``).

    Returns the full ``AuditEvent``.
    """
    plan_result = parse_prompt_plan(request.prompt)
    steps = plan_result["plan"]

    if not steps:
        raise HTTPException(status_code=400, detail="could not parse any steps from prompt")

    # Determine domain from the primary action step
    primary_step = _primary_step(steps)
    target_system = primary_step.get("target_system", "browser")

    if target_system == "browser":
        return await _execute_browser(steps, request.prompt)

    return await _execute_connector(primary_step, request.prompt)


# ── Routing helpers ─────────────────────────────────────────────────


def _primary_step(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the last non-BROWSER_OPEN step (the actual action)."""
    for step in reversed(steps):
        if step.get("action_type") != "BROWSER_OPEN":
            return step
    return steps[-1] if steps else {}


async def _execute_browser(steps: list[dict[str, Any]], prompt: str) -> dict[str, Any]:
    """
    Route a multi-step browser plan through **guardrail + Playwright**.

    **Strategy: Block-all-upfront (Opsi A).**

    Evaluates **ALL** steps in the plan through the guardrail *before*
    launching any Playwright session. If **any** step is BLOCK or
    NEED_APPROVAL, the *entire* plan is rejected — no partial execution
    occurs. Only if **every** step is ALLOW do we proceed with
    Playwright.
    """
    from app.core.action_request import build_action_request
    from app.core.schemas import Decision, ExecutionResult, ExecutionStatus
    from app.domains.audit.repositories import get_audit_repository
    from app.domains.guardrail.decision import decide

    url = _find_url(steps)
    browser_actions = _plan_to_browser_actions(steps)

    # ── Evaluate ALL steps ────────────────────────────────────────
    worst_decision: Decision = Decision.ALLOW
    worst_request = None
    worst_response = None

    for step in steps:
        proposal = {**step, "user_goal": prompt}
        request = build_action_request(proposal)
        response = decide(request)

        if response.decision == Decision.BLOCK:
            worst_decision = Decision.BLOCK
            worst_request = request
            worst_response = response
            break  # BLOCK is final — no need to continue

        if response.decision == Decision.NEED_APPROVAL:
            worst_decision = Decision.NEED_APPROVAL
            worst_request = request
            worst_response = response
            # Continue checking: there might be a BLOCK later

    # ── BLOCK / NEED_APPROVAL → reject the entire plan ────────────
    if worst_decision != Decision.ALLOW:
        status = (
            ExecutionStatus.BLOCKED
            if worst_decision == Decision.BLOCK
            else ExecutionStatus.PENDING_APPROVAL
        )
        summary = (
            "blocked by guardrail"
            if worst_decision == Decision.BLOCK
            else "pending approval"
        )
        execution = ExecutionResult(
            run_id=worst_request.run_id,
            action_id=worst_request.action_id,
            executor="router",
            status=status,
            result_summary=summary,
        )
        event = await get_audit_repository().write(
            worst_request, worst_response, execution
        )
        return event.model_dump(mode="json")

    # ── ALL steps ALLOW → proceed with Playwright ─────────────────
    from app.domains.agent.services.browser_prototype_agent import (
        run_browser_prototype_agent,
    )

    event = await run_browser_prototype_agent(
        url=url or "",
        actions=browser_actions or None,
        user_goal=prompt,
        risk_hint="unknown",
    )
    return event.model_dump(mode="json")


async def _execute_connector(step: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Route a single connector action through the guarded execution pipeline."""
    from app.domains.agent.services.guarded_execution import run_guarded_action

    proposal = {**step, "user_goal": prompt}
    audit = None  # uses default audit repository (JSONL or Postgres per settings)
    event = await run_guarded_action(proposal, audit=audit)
    return event.model_dump(mode="json")


def _find_url(steps: list[dict[str, Any]]) -> str | None:
    for step in steps:
        target = step.get("target") or ""
        if target:
            return target
    return None


def _plan_to_browser_actions(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a plan into the action format expected by ``run_browser_prototype_agent``."""
    TYPE_MAP = {
        "BROWSER_CLICK": "click",
        "BROWSER_TYPE": "fill",
        "BROWSER_SCROLL": "scroll",
        "BROWSER_SCREENSHOT": "screenshot",
        "BROWSER_SUBMIT": "click",
        "BROWSER_SELECT": "click",
    }

    actions: list[dict[str, Any]] = []
    for step in steps:
        at = step["action_type"]
        if at == "BROWSER_OPEN":
            continue
        browser_type = TYPE_MAP.get(at)
        if not browser_type:
            continue
        payload = step.get("payload", {})
        action: dict[str, Any] = {"type": browser_type}
        if payload.get("label"):
            action["label"] = payload["label"]
        if payload.get("element_id"):
            action["element_id"] = payload["element_id"]
        if payload.get("role"):
            action["role"] = payload["role"]
        if payload.get("value"):
            action["value"] = payload["value"]
        actions.append(action)

    return actions
