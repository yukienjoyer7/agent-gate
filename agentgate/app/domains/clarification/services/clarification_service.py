"""
Service layer for the ASK_USER clarification flow. Ties together:

  - PendingUserQuestionRepository (the mutable working queue)
  - the audit repository (the immutable, write-once-per-action record)
  - decide() + ExecutionRouter (to re-evaluate and, if clean, actually run
    the action once the user has responded)

so that api/v1/ask_user.py stays a thin HTTP layer, the same way
app/domains/approval/services/approval_service.py does for NEED_APPROVAL.

Unlike the approval flow, a proceed=True response can carry
payload_updates -- the user's answer can change *what's being executed*,
not just whether it runs. So resolution here re-runs decide() on the
corrected request rather than executing directly: a bad correction can
still hit BLOCK / SANITIZE / NEED_APPROVAL instead of executing blindly.

Architectural invariant: a decision only changes if the proposal changes.
proceed=True by itself carries no information about the proposal, so it
must never be treated as evidence -- there's no "user affirmed intent"
resolution path here. The payload is deep-merged with whatever
payload_updates the user supplied (possibly none) and decide() is re-run
on that merged payload; confidence is never touched. If the merge
happened to supply a field decide()'s completeness check was missing,
ASK_USER naturally won't re-trigger on its own -- no manual override
needed. If it didn't (no updates, or updates that don't fill the gap),
decide() legitimately returns ASK_USER again, exactly as it would if
called fresh on that same payload. That re-ask goes back into the
pending_user_questions queue for another round (itself unaudited, same as
the first ask) rather than failing closed after exactly one round,
bounded by MAX_CLARIFICATION_ROUNDS.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.action_request import deep_merge_payload, summarize_payload
from app.core.schemas import ActionRequest, AuditEvent, Decision, DecisionResponse, ExecutionResult, ExecutionStatus
from app.database.models.pending_user_question import PendingUserQuestion as PendingUserQuestionRow
from app.domains.approval.schemas import PendingApprovalResponse
from app.domains.approval.services import create_pending_approval
from app.domains.audit.repositories import get_audit_repository
from app.domains.clarification.repositories import PendingUserQuestionRepository
from app.domains.clarification.schemas.pending_user_question import (
    PendingUserQuestionResponse,
    UserResponseRequest,
)
from app.domains.guardrail.decision import decide
from app.executors import ExecutionRouter

# Same bound as MAX_SANITIZE_LOOPS in guarded_execution.py, applied to the
# re-decide pass after a user correction: guards a detector that can't
# fully clean its own matches, not expected to be hit in practice.
MAX_REDECIDE_LOOPS = 3

# How many rounds of ASK_USER <-> user response a single action may go
# through before the guardrail fails closed to BLOCK instead of asking
# again. Guards against a user (or a misbehaving caller) repeatedly
# responding proceed=True without ever supplying the missing information.
MAX_CLARIFICATION_ROUNDS = 3


def _to_response(row: PendingUserQuestionRow) -> PendingUserQuestionResponse:
    return PendingUserQuestionResponse(
        run_id=row.run_id,
        action_id=row.action_id,
        clarifying_question=row.question,
        clarification_round=row.clarification_round,
        request_json=row.request_json,
        decision_json=row.decision_json,
        pending_since=row.pending_since,
        expires_at=row.expires_at,
    )


async def create_pending_question(
    request: ActionRequest,
    decision: DecisionResponse,
    repo: PendingUserQuestionRepository | None = None,
) -> PendingUserQuestionResponse:
    row = await (repo or PendingUserQuestionRepository()).create(request, decision)
    return _to_response(row)


async def list_pending_questions(
    repo: PendingUserQuestionRepository | None = None,
    audit=None,
) -> list[PendingUserQuestionResponse]:
    """
    GET /ask-user. Same lazy-expiry sweep as list_pending_approvals: any
    row past expires_at is resolved to EXPIRED right here before the list
    is returned, so expired questions never show up as "pending".
    """
    repo = repo or PendingUserQuestionRepository()
    audit = audit or get_audit_repository()
    now = datetime.now(UTC)

    active: list[PendingUserQuestionResponse] = []
    for row in await repo.list():
        if row.expires_at <= now:
            await _finalize_expired(row.action_id, repo=repo, audit=audit)
        else:
            active.append(_to_response(row))
    return active


async def decide_pending_question(
    action_id: str,
    response: UserResponseRequest,
    repo: PendingUserQuestionRepository | None = None,
    audit=None,
) -> tuple[str, AuditEvent | PendingApprovalResponse | PendingUserQuestionResponse] | None:
    """
    POST /ask-user/{action_id}/respond.

    Returns None if the row doesn't exist (already resolved, expired and
    swept, or never existed) -- caller should return 404.

    Otherwise returns one of:
      ("resolved", AuditEvent)              -- cancelled, blocked, or executed
      ("escalated_to_approval", PendingApprovalResponse) -- the correction
          still needs a human reviewer; nothing written to audit_logs yet
      ("still_pending", PendingUserQuestionResponse) -- still not enough
          information to evaluate; back in the queue for another round.
          Nothing written to audit_logs yet, same as the original ask.
      ("expired", AuditEvent)                -- window passed before this call
    """
    repo = repo or PendingUserQuestionRepository()
    audit = audit or get_audit_repository()

    row = await repo.claim(action_id)
    if row is None:
        return None

    now = datetime.now(UTC)
    if row.expires_at <= now:
        event = await _resolve_expired(row, audit=audit)
        return ("expired", event)

    return await _resolve(row, response, repo=repo, audit=audit)


async def _finalize_expired(action_id: str, repo: PendingUserQuestionRepository, audit) -> None:
    row = await repo.claim(action_id)
    if row is None:
        return  # a concurrent respond()/list() call already resolved this one
    await _resolve_expired(row, audit=audit)


async def _resolve_expired(row: PendingUserQuestionRow, audit) -> AuditEvent:
    request = ActionRequest.model_validate(row.request_json)
    decision = DecisionResponse.model_validate(row.decision_json)
    pending_duration_ms = int((row.expires_at - row.pending_since).total_seconds() * 1000)
    execution = ExecutionResult(
        run_id=request.run_id,
        action_id=request.action_id,
        executor="clarification",
        status=ExecutionStatus.EXPIRED,
        result_summary="ask-user window expired",
    )
    return await audit.write(
        request,
        decision,
        execution,
        latency={"guardrail_ms": decision.latency_ms, "audit_write_ms": pending_duration_ms},
    )


async def _resolve(
    row: PendingUserQuestionRow,
    response: UserResponseRequest,
    repo: PendingUserQuestionRepository,
    audit,
) -> tuple[str, AuditEvent | PendingApprovalResponse | PendingUserQuestionResponse]:
    request = ActionRequest.model_validate(row.request_json)
    original_decision = DecisionResponse.model_validate(row.decision_json)

    if not response.proceed:
        execution = ExecutionResult(
            run_id=request.run_id,
            action_id=request.action_id,
            executor="clarification",
            status=ExecutionStatus.CANCELLED,
            result_summary="cancelled by user",
        )
        cancelled_decision = original_decision.model_copy(
            update={"reasons": original_decision.reasons + ["cancelled by user"]}
        )
        event = await audit.write(
            request,
            cancelled_decision,
            execution,
            latency={"guardrail_ms": original_decision.latency_ms},
        )
        return ("resolved", event)

    # proceed=True: merge any corrections and re-decide from scratch rather
    # than executing the original (now possibly stale, and never
    # fully-vetted) request directly. Confidence is never touched here --
    # per the "decision only changes if the proposal changes" invariant,
    # proceed=True carries no information on its own, and only a payload
    # delta can move decide() off ASK_USER. A bare proceed=True with no
    # updates leaves the payload identical, so decide() is free to
    # legitimately return ASK_USER again below.
    merged_payload = deep_merge_payload(request.payload, response.payload_updates or {})
    request = request.model_copy(
        update={
            "payload": merged_payload,
            "payload_summary": summarize_payload(merged_payload),
        }
    )
    decision = decide(request)

    sanitize_notes: list[str] = []
    sanitize_entities_seen: list[str] = []
    attempts = 0
    while decision.decision == Decision.SANITIZE and attempts < MAX_REDECIDE_LOOPS:
        sanitize_notes.append(
            f"redacted {', '.join(decision.sensitive_entities)} on pass {attempts + 1}"
        )
        sanitize_entities_seen.extend(decision.sensitive_entities)
        sanitized_payload = decision.sanitized_payload or {}
        request = request.model_copy(
            update={
                "payload": sanitized_payload,
                "payload_summary": summarize_payload(sanitized_payload),
            }
        )
        decision = decide(request)
        attempts += 1

    if decision.decision == Decision.SANITIZE:
        decision = decision.model_copy(
            update={
                "decision": Decision.BLOCK,
                "next_step": "blocked",
                "reasons": decision.reasons + ["sanitize loop limit exceeded"],
                "triggered_policies": decision.triggered_policies + ["sanitize_loop_limit_exceeded"],
            }
        )
    elif sanitize_notes:
        decision = decision.model_copy(
            update={
                "reasons": decision.reasons + sanitize_notes,
                "sensitive_entities": sorted(set(sanitize_entities_seen)),
                "triggered_policies": list(
                    dict.fromkeys(decision.triggered_policies + ["payload_sanitization_required"])
                ),
            }
        )

    if decision.decision == Decision.ASK_USER:
        # Still not enough information to evaluate. Bounded re-ask: go back
        # into the queue for another round rather than failing closed after
        # exactly one, unless the round limit is already exhausted.
        if row.clarification_round >= MAX_CLARIFICATION_ROUNDS:
            decision = decision.model_copy(
                update={
                    "decision": Decision.BLOCK,
                    "next_step": "blocked",
                    "reasons": decision.reasons
                    + [f"clarification round limit ({MAX_CLARIFICATION_ROUNDS}) exceeded"],
                    "triggered_policies": decision.triggered_policies + ["clarification_round_limit_exceeded"],
                }
            )
        else:
            # Intermediate step, same as the original ask: nothing written
            # to audit_logs, state stays owned by the clarification queue.
            new_row = await repo.create(
                request,
                decision,
                clarification_round=row.clarification_round + 1,
            )
            return ("still_pending", _to_response(new_row))

    decision = decision.model_copy(
        update={"reasons": decision.reasons + ["confirmed by user"]}
    )

    if decision.decision == Decision.NEED_APPROVAL:
        # The user's correction is still risky by risk_hint -- hand off to
        # a human reviewer instead of executing. Nothing written to
        # audit_logs yet, same as the normal NEED_APPROVAL path.
        pending = await create_pending_approval(request, decision)
        return ("escalated_to_approval", pending)

    execution = await ExecutionRouter().route(request, decision)
    event = await audit.write(
        request,
        decision,
        execution,
        latency={"guardrail_ms": decision.latency_ms, "executor_ms": execution.latency_ms},
    )
    return ("resolved", event)
