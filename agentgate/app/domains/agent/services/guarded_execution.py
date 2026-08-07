from app.core.action_request import build_action_request, summarize_payload
from app.core.schemas import ActionTrace, AuditEvent, Decision
from app.domains.approval.schemas import PendingApprovalResponse
from app.domains.approval.services import create_pending_approval
from app.domains.audit.repositories import get_audit_repository
from app.domains.clarification.schemas import PendingUserQuestionResponse
from app.domains.clarification.services import create_pending_question
from app.domains.guardrail.decision import decide
from app.executors import ExecutionRouter
from app.tracing import LatencyTracker, TraceWriter

# Safety valve for the SANITIZE re-evaluation loop below. In practice this
# resolves in 1 iteration (redact once, re-decide comes back clean) -- this
# just guards against a detector that can't fully clean its own matches
# (e.g. two overlapping patterns re-triggering each other) turning into an
# infinite loop instead of a bounded one.
MAX_SANITIZE_LOOPS = 3


async def run_guarded_action(
    proposal: dict,
    audit=None,
    traces: TraceWriter | None = None,
) -> AuditEvent | PendingApprovalResponse | PendingUserQuestionResponse:
    latency = LatencyTracker()
    latency.start("action_request")
    request = build_action_request(proposal)
    latency.stop("action_request")

    latency.start("guardrail")
    decision = decide(request)

    # SANITIZE resolves entirely within this request: redact the payload,
    # re-run decide() on the redacted version, repeat until it stops coming
    # back SANITIZE (normally 1 pass) or we hit the loop limit. Unlike
    # NEED_APPROVAL/ASK_USER (both external-actor queues below), no
    # external actor is involved here, so SANITIZE never leaves
    # guarded_execution.py -- no queue, no second endpoint.
    sanitize_notes: list[str] = []
    sanitize_entities_seen: list[str] = []
    sanitize_attempts = 0
    while decision.decision == Decision.SANITIZE and sanitize_attempts < MAX_SANITIZE_LOOPS:
        sanitize_notes.append(
            f"redacted {', '.join(decision.sensitive_entities)} on pass {sanitize_attempts + 1}"
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
        sanitize_attempts += 1

    if decision.decision == Decision.SANITIZE:
        # Loop limit hit and the payload still isn't clean -- fail closed
        # rather than either looping forever or silently executing an
        # unsanitized payload.
        decision = decision.model_copy(
            update={
                "decision": Decision.BLOCK,
                "next_step": "blocked",
                "reasons": decision.reasons + ["sanitize loop limit exceeded"],
                "triggered_policies": decision.triggered_policies + ["sanitize_loop_limit_exceeded"],
            }
        )
    elif sanitize_notes:
        # Fold the sanitize history into the final decision's reasons /
        # sensitive_entities / triggered_policies so it's visible in
        # decision_json without adding new audit_logs columns -- same
        # pattern used for reviewer metadata on the approval path.
        # sensitive_entities in particular must be re-populated here: the
        # clean re-decide pass that produced this ALLOW/NEED_APPROVAL
        # legitimately found nothing, so its own sensitive_entities is [];
        # without folding in what upstream passes found, this field would
        # contradict the "redacted ..." note already sitting in reasons.
        decision = decision.model_copy(
            update={
                "reasons": decision.reasons + sanitize_notes,
                "sensitive_entities": sorted(set(sanitize_entities_seen)),
                "triggered_policies": list(
                    dict.fromkeys(decision.triggered_policies + ["payload_sanitization_required"])
                ),
            }
        )
    latency.stop("guardrail")

    if decision.decision == Decision.NEED_APPROVAL:
        # Per pending-approval-design.md: nothing is written to audit_logs
        # yet. The action goes into the pending_approvals working queue
        # instead, and the caller gets back a PendingApprovalResponse
        # (not an AuditEvent) -- there's no execution result or final
        # audit row until a reviewer resolves it via
        # POST /approvals/{action_id}/decide.
        pending = await create_pending_approval(request, decision)
        (traces or TraceWriter()).write(
            ActionTrace(
                run_id=request.run_id,
                action_id=request.action_id,
                user_goal=proposal.get("user_goal", ""),
                raw_tool_call=proposal,
                action_request=request.model_dump(mode="json", exclude={"payload"}),
                decision=decision.model_dump(mode="json"),
                execution={"status": "PENDING_APPROVAL", "result_summary": "awaiting reviewer"},
                audit={},
                latency=latency.values(),
                final_status="PENDING_APPROVAL",
            )
        )
        return pending

    if decision.decision == Decision.ASK_USER:
        # Same shape as NEED_APPROVAL above, but for the end-user
        # clarification queue: nothing written to audit_logs yet, the
        # action goes into pending_user_questions, and the caller gets
        # back a PendingUserQuestionResponse. Resolved via
        # POST /ask-user/{action_id}/respond, which may re-decide the
        # (possibly corrected) payload rather than just executing it --
        # see app/domains/clarification/services/clarification_service.py.
        pending_question = await create_pending_question(request, decision)
        (traces or TraceWriter()).write(
            ActionTrace(
                run_id=request.run_id,
                action_id=request.action_id,
                user_goal=proposal.get("user_goal", ""),
                raw_tool_call=proposal,
                action_request=request.model_dump(mode="json", exclude={"payload"}),
                decision=decision.model_dump(mode="json"),
                execution={"status": "PENDING_USER_INPUT", "result_summary": "awaiting user response"},
                audit={},
                latency=latency.values(),
                final_status="PENDING_USER_INPUT",
            )
        )
        return pending_question

    latency.start("executor")
    execution = await ExecutionRouter().route(request, decision)
    latency.stop("executor")

    latency.start("audit_write")
    audit_latency = latency.values()
    audit_latency["audit_write_ms"] = 0
    event = await (audit or get_audit_repository()).write(request, decision, execution, audit_latency)
    latency.stop("audit_write")

    (traces or TraceWriter()).write(
        ActionTrace(
            run_id=request.run_id,
            action_id=request.action_id,
            user_goal=proposal.get("user_goal", ""),
            raw_tool_call=proposal,
            action_request=request.model_dump(mode="json", exclude={"payload"}),
            decision=decision.model_dump(mode="json"),
            execution=execution.model_dump(mode="json"),
            audit=event.model_dump(mode="json"),
            latency=latency.values(),
            final_status=execution.status,
        )
    )
    return event
