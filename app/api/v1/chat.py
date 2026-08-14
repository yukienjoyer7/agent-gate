from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from app.config.settings import get_settings
from app.core.run_schema import RunStatus
from app.domains.agent.services.agent_loop import run_agent_loop
from app.domains.agent.services.run_registry import run_registry
from app.llm.services import parse_prompt_plan

logger = logging.getLogger(__name__)

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


class RespondRequest(BaseModel):
    """User response to a paused step.

    - ``approve`` / ``decline`` — for a guardrail NEED_APPROVAL step.
    - ``input`` — text answer for a "sanitize" step (``fields`` maps payload
      key → value; ``text`` is sugar for a single-field step).
    """

    step_index: int = Field(..., ge=0, description="Index of the paused step")
    action: Literal["approve", "decline", "input"]
    text: str | None = Field(default=None, description="Free-text answer (action=input)")
    fields: dict[str, str] | None = Field(
        default=None, description="Payload key → value answers (action=input)"
    )

    @model_validator(mode="after")
    def validate_input_payload(self) -> "RespondRequest":
        if self.action == "input" and not self.fields and self.text is None:
            raise ValueError("input response requires 'fields' or 'text'")
        if self.action != "input" and (self.fields or self.text is not None):
            raise ValueError("fields/text are only valid for action=input")
        return self


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
async def execute_plan(request: ParseRequest) -> dict[str, Any]:
    """
    Start a **reactive agent run** for a natural-language instruction.

    Unlike the old parse-once/run-straight-through behaviour, the run now:

    - evaluates every step through the guardrail **one at a time**,
    - pauses for **approval** (``NEED_APPROVAL``) or **user input**
      (``sanitize`` steps) via ``POST /api/v1/chat/execute/{run_id}/respond``,
    - observes results and **re-plans** on failure / when the plan is
      exhausted (e.g. login needed, calendar data missing).

    Execution continues in the background; this endpoint returns immediately.
    Stream live events with ``POST /api/v1/chat/execute/stream``, inspect the
    live run state with ``GET /api/v1/chat/execute/{run_id}``, or poll
    ``GET /api/v1/runs/{run_id}/actions`` for the audit trail.
    """
    run = run_registry.create(request.prompt)
    run.task = asyncio.create_task(_run_with_timeout(run))
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "prompt": request.prompt,
        "stream_endpoint": "/api/v1/chat/execute/stream",
        "respond_endpoint": f"/api/v1/chat/execute/{run.run_id}/respond",
        "state_endpoint": f"/api/v1/chat/execute/{run.run_id}",
    }


@router.post("/execute/stream")
async def stream_execute(request: ParseRequest) -> StreamingResponse:
    """
    Run the reactive agent loop and **stream** every event as Server-Sent
    Events (SSE), like an AI chat:

    ``planning`` → ``plan`` → ``guardrail`` → ``step_status`` →
    ``executing`` → ``step_result`` → (``awaiting_approval`` /
    ``awaiting_input``) → ``replanning`` → ... → ``done`` / ``error``

    While a step waits for the user (approval or sanitize input), the stream
    stays open with heartbeat pings; call
    ``POST /api/v1/chat/execute/{run_id}/respond`` to resume it live.
    """
    run = run_registry.create(request.prompt)
    run.task = asyncio.create_task(_run_with_timeout(run))
    return StreamingResponse(
        _sse_generator(run),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/execute/{run_id}")
async def get_run_state(run_id: str) -> dict[str, Any]:
    """
    Live state of a run: overall status and every step's status. Useful for
    non-streaming clients to learn that a step is ``waiting_approval`` /
    ``waiting_input`` before calling the respond endpoint.
    """
    run = run_registry.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "prompt": run.prompt,
        "created_at": run.created_at.isoformat(),
        "steps": run.public_steps(),
    }


@router.post("/execute/{run_id}/respond")
async def respond_to_step(run_id: str, request: RespondRequest) -> dict[str, Any]:
    """
    Deliver a user response to a paused step:

    - ``{"action": "approve"}`` / ``{"action": "decline"}`` for
      ``waiting_approval`` steps,
    - ``{"action": "input", "fields": {"password": "..."}}`` (or ``text``)
      for ``waiting_input`` (sanitize) steps.

    The run resumes immediately; the streaming client sees the next events.
    """
    run = run_registry.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    try:
        run_registry.respond(
            run,
            request.step_index,
            request.action,
            fields=request.fields,
            text=request.text,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    step = run.step(request.step_index)
    return {
        "run_id": run.run_id,
        "step_index": request.step_index,
        "action": request.action,
        "status": "accepted",
        "step_status": step.status.value if step else None,
    }


# ── SSE plumbing ──────────────────────────────────────────────────


async def _run_with_timeout(run) -> None:
    """Run the agent loop with a hard overall deadline so a hung browser /
    connector call cannot keep the stream open forever."""
    timeout = get_settings().AGENT_RUN_TIMEOUT_SEC
    try:
        await asyncio.wait_for(run_agent_loop(run), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("run timed out after %ss: %s", timeout, run.run_id)
        run.status = RunStatus.ERROR
        run.events.put_nowait(
            {
                "type": "error",
                "data": {"run_id": run.run_id, "message": f"run timed out after {timeout}s"},
            }
        )
    except asyncio.CancelledError:
        run.status = RunStatus.CANCELLED
        raise


async def _sse_generator(run):
    """Drain the run's event queue into SSE frames; heartbeat while idle."""
    try:
        heartbeat = get_settings().SSE_HEARTBEAT_SEC
        yield _sse_frame("run_started", {"status": run.status.value}, run.run_id)
        while True:
            try:
                event = await asyncio.wait_for(run.events.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield _sse_frame(event["type"], event["data"], run.run_id)
            if event["type"] in ("done", "error"):
                break
    finally:
        # Client disconnected before the run finished → stop the loop task.
        if run.status in (RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, RunStatus.WAITING_INPUT):
            run.status = RunStatus.CANCELLED
            if run.task and not run.task.done():
                run.task.cancel()
                try:
                    await run.task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


def _sse_frame(event_type: str, data: dict[str, Any], run_id: str) -> str:
    payload = json.dumps({"run_id": run_id, "type": event_type, "data": data})
    return f"event: {event_type}\ndata: {payload}\n\n"


# ── Legacy routing helpers (kept for backward-compatible tests) ───


def _primary_step(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the last non-BROWSER_OPEN step (the actual action)."""
    for step in reversed(steps):
        if step.get("action_type") != "BROWSER_OPEN":
            return step
    return steps[-1] if steps else {}


async def _execute_browser(steps: list[dict[str, Any]], prompt: str) -> dict[str, Any]:
    """
    Legacy block-all-upfront browser execution path. Retained for the unit
    tests that cover the guardrail-rejection shape; the reactive agent loop
    (``run_agent_loop``) is the live path now.

    **Strategy: Block-all-upfront.**

    Evaluates **ALL** steps in the plan through the guardrail *before*
    launching any Playwright session. If **any** step is BLOCK or
    NEED_APPROVAL, the *entire* plan is rejected — no partial execution
    occurs. Only if **every** step is ALLOW do we proceed with Playwright.
    """
    from app.core.action_request import build_action_request
    from app.core.schemas import Decision, ExecutionResult, ExecutionStatus
    from app.domains.audit.repositories import get_audit_repository
    from app.domains.guardrail.decision import decide

    url = _find_url(steps)
    if not url:
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
        summary = "blocked by guardrail" if worst_decision == Decision.BLOCK else "pending approval"
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
    from app.domains.agent.services.browser_prototype_agent import run_browser_prototype_agent

    event = await run_browser_prototype_agent(
        url=url or "",
        actions=browser_actions or None,
        user_goal=prompt,
        risk_hint="unknown",
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
