from app.core.schemas import ActionRequest, Decision, RiskLevel
from app.domains.guardrail.decision import decide


def _action(
    risk_hint: str,
    payload: dict | None = None,
    confidence: float = 1.0,
    action_type: str = "FILE_READ",
) -> ActionRequest:
    # FILE_READ is registered in clarification/rules.py as requiring
    # "path" -- default to a complete payload so tests about risk_hint or
    # confidence aren't incidentally exercising the completeness check.
    # Tests that specifically want an incomplete proposal pass their own
    # payload without "path".
    return ActionRequest(
        action_type=action_type,
        target_system="local_file",
        target="sample.txt",
        risk_hint=risk_hint,
        payload=payload if payload is not None else {"path": "sample.txt"},
        confidence=confidence,
    )


def test_low_risk_hint_is_allowed():
    decision = decide(_action("file_read"))

    assert decision.decision == Decision.ALLOW
    assert decision.risk_level == RiskLevel.LOW
    assert decision.triggered_policies == []


def test_unknown_risk_hint_defaults_to_allow():
    decision = decide(_action("unknown"))

    assert decision.decision == Decision.ALLOW


def test_risky_hint_needs_approval():
    for risk_hint in ("external_send", "payment", "destructive", "bulk_action"):
        decision = decide(_action(risk_hint))

        assert decision.decision == Decision.NEED_APPROVAL, risk_hint
        assert decision.risk_level == RiskLevel.HIGH
        assert decision.next_step == "approval_queue"
        assert "risky_action_requires_approval" in decision.triggered_policies


def test_source_code_hint_is_hard_blocked():
    decision = decide(_action("source_code"))

    assert decision.decision == Decision.BLOCK
    assert decision.risk_level == RiskLevel.CRITICAL
    assert decision.risk_score == 1.0
    assert decision.next_step == "blocked"
    assert "source_code_egress_blocked" in decision.triggered_policies


def test_block_takes_priority_and_never_goes_to_approval_queue():
    decision = decide(_action("source_code"))

    assert decision.decision != Decision.NEED_APPROVAL
    assert decision.next_step != "approval_queue"


def test_sensitive_payload_triggers_sanitize():
    action = _action("file_read", payload={"note": "email me at foo@example.com"})
    decision = decide(action)

    assert decision.decision == Decision.SANITIZE
    assert decision.risk_level == RiskLevel.MEDIUM
    assert decision.next_step == "sanitize"
    assert "EMAIL" in decision.sensitive_entities
    assert "payload_sanitization_required" in decision.triggered_policies
    assert decision.sanitized_payload is not None
    assert "foo@example.com" not in decision.sanitized_payload["note"]


def test_sanitize_fires_regardless_of_risk_hint():
    """
    A low-risk action can still carry PII in its payload -- SANITIZE must
    fire on the sensitive-entity scan, independent of risk_hint.
    """
    action = _action("unknown", payload={"note": "ssn 123-45-6789"})
    decision = decide(action)

    assert decision.decision == Decision.SANITIZE


def test_block_takes_priority_over_sanitize():
    """
    A hard-blocked risk_hint must short-circuit before the sensitive-entity
    scan ever runs -- BLOCK wins even if the payload also has PII.
    """
    action = _action("source_code", payload={"note": "foo@example.com"})
    decision = decide(action)

    assert decision.decision == Decision.BLOCK


def test_clean_payload_does_not_trigger_sanitize():
    decision = decide(_action("file_read", payload={"path": "sample.txt"}))

    assert decision.decision == Decision.ALLOW
    assert decision.sensitive_entities == []
    assert decision.sanitized_payload is None


# --- registered action_type (FILE_READ): completeness gates ASK_USER, ---
# --- confidence is not consulted at all -----------------------------------


def test_missing_required_field_triggers_ask_user():
    decision = decide(_action("file_read", payload={}))

    assert decision.decision == Decision.ASK_USER
    assert decision.risk_level == RiskLevel.MEDIUM
    assert decision.next_step == "ask_user_queue"
    assert "insufficient_information_requires_clarification" in decision.triggered_policies
    assert "path" in decision.reasons[0]
    assert decision.clarifying_question is not None


def test_complete_payload_does_not_trigger_ask_user_regardless_of_confidence():
    """
    For a registered action_type, a complete payload should fall straight
    through to the normal decision pipeline even at very low confidence --
    confidence is not one of the ASK_USER signals for registered types.
    """
    decision = decide(_action("file_read", payload={"path": "sample.txt"}, confidence=0.1))

    assert decision.decision != Decision.ASK_USER
    assert decision.decision == Decision.ALLOW


def test_sanitize_takes_priority_over_ask_user():
    """
    An incomplete action that also carries PII must sanitize first --
    ASK_USER should never surface an unredacted payload in its queue.
    """
    action = _action("file_read", payload={"note": "foo@example.com"})
    decision = decide(action)

    assert decision.decision == Decision.SANITIZE


def test_block_takes_priority_over_ask_user():
    action = _action("source_code", payload={})
    decision = decide(action)

    assert decision.decision == Decision.BLOCK


def test_ask_user_takes_priority_over_approval_risk_hint():
    """
    An incomplete action with a risky risk_hint should ask the user before
    ever reaching a human reviewer -- the proposal must be evaluable
    before it can be scored for risk at all.
    """
    action = _action("external_send", payload={})
    decision = decide(action)

    assert decision.decision == Decision.ASK_USER


# --- unregistered action_type: legacy confidence-threshold fallback ------


def test_low_confidence_triggers_ask_user_for_unregistered_action_type():
    decision = decide(_action("file_read", action_type="CUSTOM_ACTION", confidence=0.3))

    assert decision.decision == Decision.ASK_USER
    assert decision.risk_level == RiskLevel.MEDIUM
    assert decision.next_step == "ask_user_queue"
    assert "insufficient_information_requires_clarification" in decision.triggered_policies
    assert decision.clarifying_question is not None


def test_high_confidence_does_not_trigger_ask_user_for_unregistered_action_type():
    decision = decide(_action("file_read", action_type="CUSTOM_ACTION", confidence=0.9))

    assert decision.decision != Decision.ASK_USER
    assert decision.decision == Decision.ALLOW


def test_confidence_threshold_is_exclusive_at_boundary_for_unregistered_action_type():
    from app.config.settings import get_settings

    threshold = get_settings().ASK_USER_CONFIDENCE_THRESHOLD
    decision = decide(_action("file_read", action_type="CUSTOM_ACTION", confidence=threshold))

    # confidence == threshold should NOT ask -- only strictly below it
    assert decision.decision != Decision.ASK_USER
