from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from app.api.session_context import OwnerContext, get_owner_context
from app.config.settings import get_settings
from app.core.run_schema import RunStatus, StepStatus
from app.domains.agent.services.run_registry import RunSession, run_registry
from app.domains.agent.services.run_service import start_agent_run
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


class ExecuteResponse(BaseModel):
    run_id: str = Field(..., description="Unique run identifier", examples=["run_634a174c8449"])
    status: RunStatus = Field(
        ..., description="Initial run execution status", examples=[RunStatus.RUNNING]
    )
    prompt: str = Field(..., description="Original user prompt", examples=["open example.com"])
    stream_endpoint: str = Field(
        ...,
        description="Endpoint URL to subscribe to SSE live execution stream",
        examples=["/api/v1/chat/execute/stream"],
    )
    respond_endpoint: str = Field(
        ...,
        description="Endpoint URL to submit user input or approval",
        examples=["/api/v1/chat/execute/run_634a174c8449/respond"],
    )
    state_endpoint: str = Field(
        ...,
        description="Endpoint URL to inspect current run state",
        examples=["/api/v1/chat/execute/run_634a174c8449"],
    )


class StepPublicResponse(BaseModel):
    index: int = Field(..., description="Step index (0-based)", examples=[0])
    action_id: str = Field(
        ..., description="Unique action identifier", examples=["act_a1b2c3d4e5f6"]
    )
    status: StepStatus = Field(
        ..., description="Current status of the step", examples=[StepStatus.PENDING]
    )
    data: dict[str, Any] = Field(..., description="Step action data and parameters")
    decision: dict[str, Any] | None = Field(default=None, description="Guardrail decision details")
    execution: dict[str, Any] | None = Field(default=None, description="Execution results")
    sanitize_fields: list[dict[str, Any]] | None = Field(
        default=None, description="Sanitize fields requiring input"
    )
    audit_event: dict[str, Any] | None = Field(
        default=None, description="Associated audit event data"
    )

    model_config = {"extra": "allow"}


def _step_public_response(step: dict[str, Any]) -> StepPublicResponse:
    """Adapt the flattened SSE step shape to the nested HTTP response schema."""
    envelope_fields = {
        "index",
        "action_id",
        "status",
        "data",
        "decision",
        "execution",
        "sanitize_fields",
        "audit_event",
    }
    data = step.get("data")
    if not isinstance(data, dict):
        data = {key: value for key, value in step.items() if key not in envelope_fields}

    return StepPublicResponse(
        index=step["index"],
        action_id=step["action_id"],
        status=step["status"],
        data=data,
        decision=step.get("decision"),
        execution=step.get("execution"),
        sanitize_fields=step.get("sanitize_fields"),
        audit_event=step.get("audit_event"),
    )


class RunStateResponse(BaseModel):
    run_id: str = Field(..., description="Unique run identifier", examples=["run_634a174c8449"])
    status: RunStatus = Field(
        ..., description="Current overall status of the run", examples=[RunStatus.RUNNING]
    )
    prompt: str = Field(..., description="Original prompt text", examples=["open example.com"])
    created_at: str = Field(..., description="ISO 8601 creation timestamp")
    steps: list[StepPublicResponse] = Field(default_factory=list, description="List of step states")


class RespondRequest(BaseModel):
    """User response to a paused step.

    - ``approve`` / ``decline`` — for a guardrail NEED_APPROVAL step.
    - ``input`` — text answer for a "sanitize" step (``fields`` maps payload
      key → value; ``text`` is sugar for a single-field step).
    """

    step_index: int = Field(..., ge=0, description="Index of the paused step", examples=[0])
    action: Literal["approve", "decline", "input"] = Field(
        ..., description="Response action type", examples=["approve"]
    )
    text: str | None = Field(default=None, description="Free-text answer (action=input)")
    fields: dict[str, str] | None = Field(
        default=None, description="Payload key → value answers (action=input)"
    )

    @model_validator(mode="after")
    def validate_input_payload(self) -> RespondRequest:
        if self.action == "input" and not self.fields and self.text is None:
            raise ValueError("input response requires 'fields' or 'text'")
        if self.action != "input" and (self.fields or self.text is not None):
            raise ValueError("fields/text are only valid for action=input")
        return self


class RespondResponse(BaseModel):
    run_id: str = Field(..., description="Run identifier", examples=["run_634a174c8449"])
    step_index: int = Field(..., description="Step index that was responded to", examples=[0])
    action: Literal["approve", "decline", "input"] = Field(
        ..., description="Action delivered", examples=["approve"]
    )
    status: Literal["accepted"] = Field(
        default="accepted", description="Delivery status", examples=["accepted"]
    )
    step_status: StepStatus | None = Field(default=None, description="Updated status of the step")


@router.post(
    "/parse",
    response_model=ParseResponse,
    summary="Parse Prompt Plan",
    description="Parse a natural-language instruction into an AI-generated plan containing atomic steps.",
)
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


@router.post(
    "/execute",
    response_model=ExecuteResponse,
    summary="Execute Reactive Agent Run",
    description="Start a reactive agent run in the background for a natural-language instruction and return run tracking endpoints.",
)
async def execute_plan(
    request: ParseRequest,
    owner: OwnerContext = Depends(get_owner_context),
) -> ExecuteResponse:
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
    run = start_agent_run(
        request.prompt,
        metadata={"owner_id": owner.owner_id, "session_id": owner.session_id},
    )
    return ExecuteResponse(
        run_id=run.run_id,
        status=run.status,
        prompt=request.prompt,
        stream_endpoint="/api/v1/chat/execute/stream",
        respond_endpoint=f"/api/v1/chat/execute/{run.run_id}/respond",
        state_endpoint=f"/api/v1/chat/execute/{run.run_id}",
    )


@router.post(
    "/execute/stream",
    summary="Stream Reactive Agent Execution (SSE)",
    description="Run the reactive agent loop and stream real-time events as Server-Sent Events (SSE).",
    responses={
        200: {
            "description": "Server-Sent Events stream of agent lifecycle events",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def stream_execute(
    request: ParseRequest,
    owner: OwnerContext = Depends(get_owner_context),
) -> StreamingResponse:
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
    run = start_agent_run(
        request.prompt,
        metadata={"owner_id": owner.owner_id, "session_id": owner.session_id},
    )
    return StreamingResponse(
        _sse_generator(run),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get(
    "/execute/{run_id}",
    response_model=RunStateResponse,
    summary="Get Run State",
    description="Retrieve the current live state of a run, including overall status and individual step states.",
    responses={404: {"description": "Run not found"}},
)
async def get_run_state(
    run_id: str = Path(..., description="Unique run identifier", examples=["run_634a174c8449"]),
    owner: OwnerContext = Depends(get_owner_context),
) -> RunStateResponse:
    """
    Live state of a run: overall status and every step's status. Useful for
    non-streaming clients to learn that a step is ``waiting_approval`` /
    ``waiting_input`` before calling the respond endpoint.
    """
    run = run_registry.get(run_id)
    if run is None or not _run_belongs_to_owner(run, owner):
        raise HTTPException(status_code=404, detail="run not found")
    return RunStateResponse(
        run_id=run.run_id,
        status=run.status,
        prompt=run.prompt,
        created_at=run.created_at.isoformat(),
        steps=[_step_public_response(s) for s in run.public_steps()],
    )


@router.post(
    "/execute/{run_id}/respond",
    response_model=RespondResponse,
    summary="Respond to Paused Step",
    description="Deliver a user decision (approve/decline) or input values for a step that is paused waiting for user action.",
    responses={
        404: {"description": "Run or step not found"},
        409: {"description": "Step is not currently waiting for a response"},
    },
)
async def respond_to_step(
    request: RespondRequest,
    run_id: str = Path(..., description="Unique run identifier", examples=["run_634a174c8449"]),
    owner: OwnerContext = Depends(get_owner_context),
) -> RespondResponse:
    """
    Deliver a user response to a paused step:

    - ``{"action": "approve"}`` / ``{"action": "decline"}`` for
      ``waiting_approval`` steps,
    - ``{"action": "input", "fields": {"password": "..."}}`` (or ``text``)
      for ``waiting_input`` (sanitize) steps.

    The run resumes immediately; the streaming client sees the next events.
    """
    run = run_registry.get(run_id)
    if run is None or not _run_belongs_to_owner(run, owner):
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
    return RespondResponse(
        run_id=run.run_id,
        step_index=request.step_index,
        action=request.action,
        status="accepted",
        step_status=step.status if step else None,
    )


def _run_belongs_to_owner(run: RunSession, owner: OwnerContext) -> bool:
    run_owner = str(run.metadata.get("owner_id") or "default")
    if run_owner != owner.owner_id:
        return False
    if owner.session_id is not None:
        return run.metadata.get("session_id") == owner.session_id
    return run.metadata.get("session_id") is None


# ── SSE plumbing ──────────────────────────────────────────────────


async def _sse_generator(run):
    """Drain the run's event queue into SSE frames; heartbeat while idle."""
    try:
        heartbeat = get_settings().SSE_HEARTBEAT_SEC
        yield _sse_frame("run_started", {"status": run.status.value}, run.run_id)
        while True:
            try:
                event = await asyncio.wait_for(run.events.get(), timeout=heartbeat)
            except TimeoutError:
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
                except asyncio.CancelledError:
                    logger.debug(
                        "run task cancelled after SSE disconnect", extra={"run_id": run.run_id}
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "run task stopped after SSE disconnect",
                        extra={"run_id": run.run_id, "error": str(exc)[:200]},
                    )


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
        assert worst_request is not None
        assert worst_response is not None
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
