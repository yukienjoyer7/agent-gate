from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config.settings import get_settings
from app.core.schemas import Decision
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
    result = await parse_prompt_plan(request.prompt)
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
    plan_result = await parse_prompt_plan(request.prompt)
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
    if not url:
        # Keep the AuditEvent response shape (consumers read execution_status /
        # execution_json) instead of a bare 400 detail — this path triggers
        # whenever the rule-based fallback parses a URL-less browser plan.
        from app.core.action_request import build_action_request
        from app.core.schemas import ExecutionResult, ExecutionStatus
        from app.domains.audit.repositories import get_audit_repository
        from app.domains.guardrail.decision import decide

        proposal = {**(steps[0] if steps else {}), "user_goal": prompt}
        request = build_action_request(proposal)
        response = decide(request)
        execution = ExecutionResult(
            run_id=request.run_id,
            action_id=request.action_id,
            executor="router",
            status=ExecutionStatus.FAILED,
            result_summary="plan has no browser URL to open (check the parsed plan)",
            error={
                "code": "INVALID_PLAN",
                "message": "plan has no browser URL to open (check the parsed plan)",
            },
        )
        event = await get_audit_repository().write(request, response, execution)
        return event.model_dump(mode="json")

    browser_actions = _plan_to_browser_actions(steps)

    # ── Evaluate ALL steps ────────────────────────────────────────
    worst_decision: Decision = Decision.ALLOW
    worst_request = None
    worst_response = None

    for step in steps:
        proposal = {**step, "user_goal": prompt}
        request = build_action_request(proposal)
        response = decide(request)

        # Any non-ALLOW decision halts the whole plan (block-all-upfront).
        # BLOCK is final; the others keep scanning in case a worse decision
        # (BLOCK > NEED_APPROVAL > SANITIZE > ASK_USER) appears later.
        if response.decision != Decision.ALLOW and _decision_priority(
            response.decision
        ) > _decision_priority(worst_decision):
            worst_decision = response.decision
            worst_request = request
            worst_response = response
        if response.decision == Decision.BLOCK:
            break

    # ── Any non-ALLOW decision → reject the entire plan ───────────
    if worst_decision != Decision.ALLOW:
        from app.executors.router import decision_to_execution_status

        status = decision_to_execution_status(worst_decision)
        summary = {
            Decision.BLOCK: "blocked by guardrail",
            Decision.NEED_APPROVAL: "pending approval",
            Decision.SANITIZE: "sanitized payload ready; awaiting confirmation",
            Decision.ASK_USER: "clarification required from user before execution",
        }[worst_decision]
        execution = ExecutionResult(
            run_id=worst_request.run_id,
            action_id=worst_request.action_id,
            executor="router",
            status=status,
            result_summary=summary,
        )
        event = await get_audit_repository().write(worst_request, worst_response, execution)
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
        # SPA pages (e.g. YouTube) render interactive headers slightly after
        # domcontentloaded; settle briefly so the search box is in the snapshot
        # before elements are resolved against it. Kept in lockstep with the
        # planner tool via BROWSER_SETTLE_MS.
        settle_ms=get_settings().BROWSER_SETTLE_MS,
    )
    return event.model_dump(mode="json")


async def _execute_connector(step: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Route a single connector action through the guarded execution pipeline."""
    from app.domains.agent.services.guarded_execution import run_guarded_action

    proposal = {**step, "user_goal": prompt}
    audit = None  # uses default audit repository (JSONL or Postgres per settings)
    event = await run_guarded_action(proposal, audit=audit)
    return event.model_dump(mode="json")


def _decision_priority(decision: Decision) -> int:
    """Order of severity used by the block-all-upfront plan guard.
    Higher wins when multiple steps carry different non-ALLOW decisions."""
    return {
        Decision.ALLOW: 0,
        Decision.ASK_USER: 1,
        Decision.SANITIZE: 2,
        Decision.NEED_APPROVAL: 3,
        Decision.BLOCK: 4,
    }[decision]


def _find_url(steps: list[dict[str, Any]]) -> str | None:
    for step in steps:
        target = step.get("target") or ""
        if target:
            return target
    return None


def _plan_to_browser_actions(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a plan into the action format expected by ``run_browser_prototype_agent``."""
    from app.domains.agent.services.browser_prototype_agent import plan_step_to_browser_action

    actions = [plan_step_to_browser_action(step) for step in steps]
    return [action for action in actions if action]
