"""Reactive agent loop orchestrator.

Turns ``/chat/execute`` from a "parse once, run straight through" pipeline
into a plan-then-react loop::

    plan -> (guardrail -> approve / sanitize / execute -> observe -> replan)*

- The initial plan is the strategy. Steps are evaluated by the guardrail
  **one at a time** before anything executes (replacing the old
  block-all-upfront strategy).
- ``NEED_APPROVAL`` steps pause and wait for the user via
  ``POST /chat/execute/{run_id}/respond`` (approve / decline).
- Sensitive steps ("sanitize") pause and wait for the user to type a missing
  value (password, API key, ``{{placeholder}}``) via the same endpoint.
- After a failure, or when the plan is exhausted, the LLM replanner decides
  the next step(s) from the observation — enabling branches like "login
  needed" or "calendar returned no events → do something else".

Every event is pushed onto the run session's queue; the SSE endpoint drains
it live. Audit events are written through the existing repositories (one per
executed/blocked/declined action).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.config.settings import get_settings
from app.core.action_request import build_action_request
from app.core.run_schema import RunStatus, StepStatus
from app.core.schemas import (
    ActionRequest,
    Decision,
    DecisionResponse,
    ExecutionResult,
    ExecutionStatus,
    new_id,
)
from app.domains.agent.services.agent_planner import parse_next_steps
from app.domains.agent.services.browser_prototype_agent import (
    plan_step_to_browser_action,
    run_browser_prototype_agent,
)
from app.domains.agent.services.guarded_execution import run_guarded_action
from app.domains.agent.services.run_registry import RunSession, StepState
from app.domains.audit.repositories import get_audit_repository
from app.domains.guardrail.decision import adecide
from app.domains.guardrail.sensitive import detect_sensitive_fields
from app.llm.services import parse_prompt_plan

logger = logging.getLogger(__name__)


async def run_agent_loop(run: RunSession) -> None:
    """Drive one run to completion, emitting events as it goes."""
    settings = get_settings()
    try:
        await _plan(run)

        while len(run.steps) < settings.AGENT_MAX_STEPS:
            batch = _next_batch(run)
            if batch is None:
                if not await _maybe_replan(run):
                    break
                continue

            outcome = await _execute_batch(run, batch)
            if outcome == "stop":
                break
            if outcome == "had_failure" or _all_processed(run):
                if not await _maybe_replan(run):
                    break
    except Exception as exc:  # noqa: BLE001 - surface everything to the client
        logger.exception("agent loop failed for run %s", run.run_id)
        run.status = RunStatus.ERROR
        _emit(run, "error", {"run_id": run.run_id, "message": str(exc)[:500]})
        return

    if run.status == RunStatus.RUNNING:
        run.status = RunStatus.DONE
    _emit(
        run, "done", {"run_id": run.run_id, "status": run.status.value, "steps": run.public_steps()}
    )


# ── Planning & replanning ─────────────────────────────────────────


async def _plan(run: RunSession) -> None:
    _emit(run, "planning", {"run_id": run.run_id})
    result = await parse_prompt_plan(run.prompt)
    steps = result.get("plan") or []
    if not steps:
        raise ValueError("could not parse any steps from prompt")

    for data in steps:
        run.steps.append(StepState(index=len(run.steps), data=data, action_id=new_id("act")))
    _emit_plan(run, result)


async def _maybe_replan(run: RunSession) -> bool:
    """Ask the replanner for the next step(s). False = run is complete."""
    settings = get_settings()
    if run.replan_count >= settings.AGENT_MAX_REPLAN:
        return False
    if not run.steps and not run.execution_log:
        return False

    run.replan_count += 1
    _emit(run, "replanning", {"run_id": run.run_id, "iteration": run.replan_count})

    context = {
        "executed_steps": run.execution_log,
        "latest_observation": run.last_observation,
        "remaining_plan": [
            step.public() for step in run.steps if step.status == StepStatus.PENDING
        ],
    }
    next_steps = await parse_next_steps(run.prompt, context)
    for data in next_steps:
        run.steps.append(StepState(index=len(run.steps), data=data, action_id=new_id("act")))
    if next_steps:
        _emit_plan(run)
    return bool(next_steps)


def _emit_plan(run: RunSession, result: dict[str, Any] | None = None) -> None:
    _emit(
        run,
        "plan",
        {
            "run_id": run.run_id,
            "plan": run.public_steps(),
            "summary": (result or {}).get("human_readable", ""),
            "llm_provider": (result or {}).get("llm_provider"),
            "raw_prompt": run.prompt,
        },
    )


# ── Batch execution ───────────────────────────────────────────────


def _next_batch(run: RunSession) -> list[StepState] | None:
    """The next executable chunk: consecutive browser steps sharing a URL, or
    a single connector step. None when nothing is pending."""
    pending = [step for step in run.steps if step.status == StepStatus.PENDING]
    if not pending:
        return None
    first = pending[0]
    if first.data.get("target_system") != "browser":
        return [first]

    url = str(first.data.get("target") or "")
    batch: list[StepState] = []
    for step in pending:
        if step.data.get("target_system") != "browser":
            break
        if str(step.data.get("target") or "") != url:
            break
        batch.append(step)
    return batch


async def _execute_batch(run: RunSession, batch: list[StepState]) -> str:
    """Guardrail every step in the batch, then execute. Returns
    ``"stop"`` (terminal), ``"had_failure"`` or ``"executed"``."""
    executable: list[tuple[StepState, DecisionResponse]] = []
    for step in batch:
        decision = await _guardrail_step(run, step)
        if decision is None:  # blocked / declined / input timeout
            return "stop"
        executable.append((step, decision))

    if not executable:
        return "executed"

    first = executable[0][0]
    if first.data.get("target_system") == "browser":
        return await _execute_browser_batch(run, [step for step, _ in executable])
    return await _execute_connector_steps(run, executable)


async def _guardrail_step(run: RunSession, step: StepState) -> DecisionResponse | None:
    """Run the hybrid guardrail for one step, handling approval and sanitize
    pauses. Returns the (possibly approved) decision, or None when the run
    must stop."""
    settings = get_settings()
    while True:
        _set_status(run, step, StepStatus.RUNNING)
        request = _action_request(run, step)
        decision = await adecide(request)
        step.decision = decision.model_dump(mode="json")
        _emit(run, "guardrail", _decision_event(run, step, decision))

        if decision.decision == Decision.BLOCK:
            await _write_skipped_audit(
                run, step, decision, ExecutionStatus.BLOCKED, "blocked by guardrail"
            )
            run.status = RunStatus.BLOCKED
            _set_status(run, step, StepStatus.BLOCKED)
            return None

        if decision.decision == Decision.NEED_APPROVAL:
            step.status = StepStatus.WAITING_APPROVAL
            run.status = RunStatus.WAITING_APPROVAL
            _set_status(run, step, StepStatus.WAITING_APPROVAL)
            _emit(run, "awaiting_approval", _decision_event(run, step, decision))
            response = await _wait_for_user(run, step, settings.AGENT_WAIT_RESPONSE_TIMEOUT_SEC)
            if response is None:  # timeout
                return None
            run.status = RunStatus.RUNNING
            if response["action"] == "decline":
                await _write_skipped_audit(
                    run, step, decision, ExecutionStatus.SKIPPED, "declined by user"
                )
                run.status = RunStatus.DECLINED
                _set_status(run, step, StepStatus.DECLINED)
                return None
            step.status = StepStatus.APPROVED
            _set_status(run, step, StepStatus.APPROVED)
            decision = decision.model_copy(
                update={
                    "decision": Decision.ALLOW,
                    "reasons": [*decision.reasons, "approved by user"],
                    "next_step": "execute",
                }
            )
            step.decision = decision.model_dump(mode="json")
            return decision

        # Sanitize: does the step need a value the user must type?
        fields = detect_sensitive_fields(request.payload, step.answered)
        if not fields and decision.sanitized_payload:
            fields = [
                {"key": str(key), "label": str(key).replace("_", " ").title()}
                for key in decision.sanitized_payload
            ]
        if fields:
            step.sanitize_fields = fields
            step.status = StepStatus.WAITING_INPUT
            run.status = RunStatus.WAITING_INPUT
            _set_status(run, step, StepStatus.WAITING_INPUT)
            _emit(
                run,
                "awaiting_input",
                {
                    "run_id": run.run_id,
                    "index": step.index,
                    "sanitize": True,
                    "fields": fields,
                    "step": step.public(),
                },
            )
            response = await _wait_for_user(run, step, settings.AGENT_WAIT_RESPONSE_TIMEOUT_SEC)
            if response is None:  # timeout
                return None
            run.status = RunStatus.RUNNING
            _apply_user_input(step, response)
            # Re-evaluate with the filled payload before executing.
            continue

        return decision


# ── Execution ─────────────────────────────────────────────────────


async def _execute_browser_batch(run: RunSession, steps: list[StepState]) -> str:
    url = str(steps[0].data.get("target") or (steps[0].data.get("payload") or {}).get("url") or "")
    actions = [
        action for step in steps if (action := plan_step_to_browser_action(step.data)) is not None
    ]

    if not url:
        for step in steps:
            await _write_skipped_audit(
                run,
                step,
                _stored_decision(run, step),
                ExecutionStatus.FAILED,
                "plan has no browser URL to open (check the parsed plan)",
            )
            _set_status(run, step, StepStatus.FAILED)
        run.execution_log.append(
            {
                "index": [step.index for step in steps],
                "action_type": steps[0].data.get("action_type"),
                "status": "failed",
                "summary": "plan has no browser URL to open",
            }
        )
        run.last_observation = "no browser URL in plan"
        return "had_failure"

    _emit(
        run,
        "executing",
        {
            "run_id": run.run_id,
            "index": [step.index for step in steps],
            "target_system": "browser",
            "url": url,
            "actions": actions,
        },
    )

    error_message = ""
    try:
        event = await run_browser_prototype_agent(
            url=url,
            actions=actions or None,
            user_goal=run.prompt,
            risk_hint="unknown",
            run_id=run.run_id,
            action_id=steps[0].action_id,
            skip_guardrail=True,
            settle_ms=get_settings().BROWSER_SETTLE_MS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("browser batch failed for run %s", run.run_id)
        event = None
        error_message = str(exc)[:300]

    ok = event is not None and event.execution_status == ExecutionStatus.SUCCESS
    execution = event.execution_json if event else {}
    for step in steps:
        step.status = StepStatus.DONE if ok else StepStatus.FAILED
        step.execution = execution
        step.audit_event = event.model_dump(mode="json") if event else None
        _set_status(run, step, step.status)

    observation = _browser_observation(event, error_message)
    run.last_observation = observation
    run.execution_log.append(
        {
            "index": [step.index for step in steps],
            "action_type": steps[0].data.get("action_type"),
            "status": "done" if ok else "failed",
            "summary": (execution or {}).get("result_summary", ""),
        }
    )
    _emit(
        run,
        "step_result",
        {
            "run_id": run.run_id,
            "index": [step.index for step in steps],
            "status": (event.execution_status.value if event else "FAILED"),
            "result_summary": (execution or {}).get("result_summary", error_message),
            "observation": observation,
        },
    )
    return "had_failure" if not ok else "executed"


async def _execute_connector_steps(
    run: RunSession, executable: list[tuple[StepState, DecisionResponse]]
) -> str:
    had_failure = False
    for step, decision in executable:
        _emit(
            run,
            "executing",
            {
                "run_id": run.run_id,
                "index": step.index,
                "target_system": step.data.get("target_system"),
                "action_type": step.data.get("action_type"),
            },
        )
        proposal = {
            **step.data,
            "user_goal": run.prompt,
            "run_id": run.run_id,
            "action_id": step.action_id,
        }
        error_message = ""
        try:
            event = await run_guarded_action(proposal, decision=decision)
        except Exception as exc:  # noqa: BLE001
            logger.exception("connector step failed for run %s", run.run_id)
            event = None
            error_message = str(exc)[:300]

        ok = event is not None and event.execution_status == ExecutionStatus.SUCCESS
        step.status = StepStatus.DONE if ok else StepStatus.FAILED
        step.execution = event.execution_json if event else None
        step.audit_event = event.model_dump(mode="json") if event else None
        _set_status(run, step, step.status)

        observation = _connector_observation(event, error_message)
        run.last_observation = observation
        run.execution_log.append(
            {
                "index": step.index,
                "action_type": step.data.get("action_type"),
                "status": "done" if ok else "failed",
                "summary": (step.execution or {}).get("result_summary", ""),
            }
        )
        _emit(
            run,
            "step_result",
            {
                "run_id": run.run_id,
                "index": step.index,
                "status": (event.execution_status.value if event else "FAILED"),
                "result_summary": (step.execution or {}).get("result_summary", error_message),
                "observation": observation,
            },
        )
        if not ok:
            had_failure = True
    return "had_failure" if had_failure else "executed"


# ── Helpers ───────────────────────────────────────────────────────


def _action_request(run: RunSession, step: StepState) -> ActionRequest:
    proposal = {
        **step.data,
        "user_goal": run.prompt,
        "run_id": run.run_id,
        "action_id": step.action_id,
    }
    return build_action_request(proposal)


async def _wait_for_user(run: RunSession, step: StepState, timeout: float) -> dict[str, Any] | None:
    """Pause until the respond endpoint resolves this step. None = timeout."""
    pending = run.pending_responses.pop(step.index, None)
    if pending is not None:
        return pending

    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    run.waiters[step.index] = future
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        run.waiters.pop(step.index, None)
        run.pending_responses.pop(step.index, None)
        await _write_skipped_audit(
            run,
            step,
            _stored_decision(run, step),
            ExecutionStatus.FAILED,
            "timed out waiting for user response",
        )
        run.status = RunStatus.FAILED
        _set_status(run, step, StepStatus.FAILED)
        _emit(
            run, "error", {"run_id": run.run_id, "message": "timed out waiting for user response"}
        )
        return None


def _apply_user_input(step: StepState, response: dict[str, Any]) -> None:
    payload = step.data.setdefault("payload", {})
    fields = response.get("fields") or {}
    if fields:
        for key, value in fields.items():
            payload[key] = value
            step.answered.add(key)
    elif response.get("text") is not None and step.sanitize_fields:
        key = step.sanitize_fields[0]["key"]
        payload[key] = response["text"]
        step.answered.add(key)
    step.sanitize_fields = None


def _stored_decision(run: RunSession, step: StepState) -> DecisionResponse:
    """Rehydrate the step's stored decision, falling back to a safe ALLOW."""
    if step.decision:
        try:
            return DecisionResponse.model_validate(step.decision)
        except Exception:  # noqa: BLE001 - best-effort rehydration
            pass
    return DecisionResponse(
        run_id=run.run_id,
        action_id=step.action_id,
        decision=Decision.ALLOW,
        reasons=["decision not recorded"],
    )


async def _write_skipped_audit(
    run: RunSession,
    step: StepState,
    decision: DecisionResponse,
    status: ExecutionStatus,
    summary: str,
) -> None:
    execution = ExecutionResult(
        run_id=run.run_id,
        action_id=step.action_id,
        executor="agent_loop",
        status=status,
        result_summary=summary,
    )
    try:
        request = _action_request(run, step)
        event = await get_audit_repository().write(request, decision, execution)
        step.audit_event = event.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 - audit failure must not kill the loop
        logger.warning("could not write audit event for run %s: %s", run.run_id, exc)


def _set_status(run: RunSession, step: StepState, status: StepStatus) -> None:
    step.status = status
    _emit(run, "step_status", {"run_id": run.run_id, "index": step.index, "status": status.value})


def _emit(run: RunSession, event_type: str, data: dict[str, Any]) -> None:
    run.events.put_nowait({"type": event_type, "data": data})


def _decision_event(run: RunSession, step: StepState, decision: DecisionResponse) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "index": step.index,
        "decision": decision.decision.value,
        "risk_level": decision.risk_level.value,
        "risk_score": decision.risk_score,
        "reasons": decision.reasons,
        "triggered_policies": decision.triggered_policies,
        "step": step.public(),
    }


def _all_processed(run: RunSession) -> bool:
    return all(step.status != StepStatus.PENDING for step in run.steps)


def _browser_observation(event, error_message: str) -> str:
    if event is None:
        return f"error={error_message}"
    data = event.execution_json.get("data") or {}
    parts: list[str] = []
    if data.get("final_url"):
        parts.append(f"final_url={data['final_url']}")
    results = data.get("action_results") or []
    if results:
        parts.append(
            "actions="
            + json.dumps(
                [{k: r.get(k) for k in ("index", "type", "status")} for r in results],
                ensure_ascii=False,
            )
        )
    snapshot = data.get("final_snapshot") or data.get("snapshot") or []
    elements = [f"{e.get('role')}:{e.get('label')}" for e in snapshot[:15]]
    if elements:
        parts.append("visible=" + ", ".join(elements))
    error = event.execution_json.get("error") or {}
    if error.get("message"):
        parts.append(f"error={str(error['message'])[:300]}")
    return "; ".join(parts)


def _connector_observation(event, error_message: str) -> str:
    if event is None:
        return f"error={error_message}"
    parts = [f"status={event.execution_status.value}"]
    summary = event.execution_json.get("result_summary")
    if summary:
        parts.append(f"summary={summary}")
    data = event.execution_json.get("data")
    if data is not None:
        parts.append("data=" + json.dumps(data, ensure_ascii=False)[:500])
    error = event.execution_json.get("error") or {}
    if error.get("message"):
        parts.append(f"error={str(error['message'])[:300]}")
    return "; ".join(parts)
