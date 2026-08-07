from time import perf_counter

from app.config.settings import get_settings
from app.core.schemas import ActionRequest, Decision, DecisionResponse, RiskLevel
from app.domains.clarification.rules import check_completeness, is_registered
from app.domains.guardrail.detectors import scan_and_sanitize

# risk_hint values that are hard-blocked regardless of approval, per the
# sprint.md ActionRequest contract (3.1) risk_hint vocabulary and the PRD's
# Approval Manager & SafetyGuard section ("Block confidential file access,
# source-code egress..."). Unlike NEED_APPROVAL, these never reach a human
# reviewer -- there's no queue, no wait, the request is rejected the moment
# decide() sees it. ExecutionRouter.route() already has a BLOCK branch
# (app/executors/router.py) that turns this into a BLOCKED ExecutionResult
# without calling a connector, and guarded_execution.py writes that straight
# to the audit log like any other non-approval decision -- so this is the
# only change needed to make BLOCK reachable end-to-end.
BLOCKED_RISK_HINTS = {"source_code"}

# risk_hint values that require a human reviewer before executing.
APPROVAL_RISK_HINTS = {"external_send", "payment", "destructive", "bulk_action"}


def decide(action: ActionRequest) -> DecisionResponse:
    started = perf_counter()

    if action.risk_hint in BLOCKED_RISK_HINTS:
        return DecisionResponse(
            run_id=action.run_id,
            action_id=action.action_id,
            decision=Decision.BLOCK,
            risk_level=RiskLevel.CRITICAL,
            risk_score=1.0,
            reasons=[f"risk_hint={action.risk_hint}"],
            triggered_policies=["source_code_egress_blocked"],
            next_step="blocked",
            latency_ms=int((perf_counter() - started) * 1000),
        )

    # Sensitive-entity check runs before the approval-risk-hint check, on
    # every action regardless of risk_hint -- a low-risk action can still
    # carry PII/secrets in its payload. guarded_execution.py re-runs decide()
    # on the redacted payload after this fires, so on the next pass the scan
    # comes back clean (assuming full redaction) and falls through to the
    # ALLOW/NEED_APPROVAL branch below as normal.
    sanitized_payload, sensitive_entities = scan_and_sanitize(action.payload)
    if sensitive_entities:
        return DecisionResponse(
            run_id=action.run_id,
            action_id=action.action_id,
            decision=Decision.SANITIZE,
            risk_level=RiskLevel.MEDIUM,
            risk_score=0.5,
            reasons=[f"sensitive entities detected: {', '.join(sensitive_entities)}"],
            triggered_policies=["payload_sanitization_required"],
            sensitive_entities=sensitive_entities,
            sanitized_payload=sanitized_payload,
            next_step="sanitize",
            latency_ms=int((perf_counter() - started) * 1000),
        )

    # ASK_USER: the proposal is structurally incomplete -- the guardrail
    # cannot evaluate it at all, which is *not* a risk judgment, so this
    # runs before the risk_hint / approval check below (an action can't be
    # scored for risk until it's well-formed enough to evaluate). It runs
    # after the sanitize check above so PII never sits in the ask-user queue
    # unredacted. The user is expected to complete the proposal (see
    # PendingUserQuestionResponse / clarification_service) -- not merely
    # confirm it, which is what keeps ASK_USER from collapsing back into
    # NEED_APPROVAL with an extra step.
    #
    # Two disjoint paths, deliberately not combined into one signal:
    #
    #   - action_type registered in clarification/rules.py: completeness
    #     (are the required fields present?) is the only signal. Confidence
    #     is not consulted here, because the only thing that can resolve
    #     ASK_USER is the proposal itself changing, and confidence is a
    #     static field nothing here derives or updates. If confidence also
    #     gated registered types, a proposal could stay stuck in ASK_USER
    #     forever even after the missing field was supplied.
    #   - action_type not registered: there's no known field shape to check,
    #     so this falls back to the confidence threshold exactly as before
    #     this change. Because nothing raises confidence after the fact
    #     anymore (see clarification_service._resolve), this fallback will
    #     not resolve via clarification -- it re-asks up to
    #     MAX_CLARIFICATION_ROUNDS and then fails closed to BLOCK. That is
    #     intended under "decision only changes if the proposal changes":
    #     an unregistered action_type has nothing the user could supply to
    #     fix it.
    if is_registered(action.action_type):
        is_complete, missing_fields = check_completeness(action)
        if not is_complete:
            return DecisionResponse(
                run_id=action.run_id,
                action_id=action.action_id,
                decision=Decision.ASK_USER,
                risk_level=RiskLevel.MEDIUM,
                risk_score=round(1 - action.confidence, 2),
                reasons=[f"missing required fields: {', '.join(missing_fields)}"],
                triggered_policies=["insufficient_information_requires_clarification"],
                clarifying_question=(
                    f"I need {', '.join(missing_fields)} to evaluate this "
                    f"{action.action_type} on {action.target}."
                ),
                next_step="ask_user_queue",
                latency_ms=int((perf_counter() - started) * 1000),
            )
    else:
        threshold = get_settings().ASK_USER_CONFIDENCE_THRESHOLD
        if action.confidence < threshold:
            return DecisionResponse(
                run_id=action.run_id,
                action_id=action.action_id,
                decision=Decision.ASK_USER,
                risk_level=RiskLevel.MEDIUM,
                risk_score=round(1 - action.confidence, 2),
                reasons=[f"confidence {action.confidence:.2f} below threshold {threshold:.2f}"],
                triggered_policies=["insufficient_information_requires_clarification"],
                clarifying_question=(
                    f"I don't have enough information to evaluate this {action.action_type} "
                    f"on {action.target} (confidence {action.confidence:.0%}, need "
                    f"{threshold:.0%}). Can you clarify or fill in the missing details?"
                ),
                next_step="ask_user_queue",
                latency_ms=int((perf_counter() - started) * 1000),
            )

    # By this point the proposal has passed the ASK_USER check above, so the
    # guardrail has enough information to evaluate it. NEED_APPROVAL means
    # that evaluation puts the risk above the autonomous-execution threshold
    # (risk_hint in APPROVAL_RISK_HINTS) without reaching BLOCK territory
    # (BLOCKED_RISK_HINTS, checked earlier) -- a human must authorize before
    # execution rather than the request being rejected outright.
    risky = action.risk_hint in APPROVAL_RISK_HINTS
    return DecisionResponse(
        run_id=action.run_id,
        action_id=action.action_id,
        decision=Decision.NEED_APPROVAL if risky else Decision.ALLOW,
        risk_level=RiskLevel.HIGH if risky else RiskLevel.LOW,
        risk_score=0.8 if risky else 0.1,
        reasons=[f"risk_hint={action.risk_hint}"],
        triggered_policies=["risky_action_requires_approval"] if risky else [],
        next_step="approval_queue" if risky else "execute",
        latency_ms=int((perf_counter() - started) * 1000),
    )
