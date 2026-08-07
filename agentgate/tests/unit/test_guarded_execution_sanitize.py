import asyncio

from app.core.schemas import Decision
from app.domains.audit.repositories import AuditRepository


def test_sanitized_low_risk_action_executes_with_redacted_payload(tmp_path, monkeypatch):
    """
    A low-risk action carrying PII must: get redacted, re-decide to ALLOW,
    execute with the REDACTED payload (not the original), and write a
    single audit row whose decision_json shows the sanitize history.
    """
    from app.domains.agent.services import guarded_execution

    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))

    event = asyncio.run(
        guarded_execution.run_guarded_action(
            {
                "action_type": "FILE_READ",
                "target_system": "local_file",
                "target": "sample.txt",
                "risk_hint": "file_read",
                "payload": {
                    "action": "read",
                    "path": "sample.txt",
                    "note": "contact me at foo@example.com",
                },
            },
            audit=AuditRepository(str(tmp_path / "audit.jsonl")),
        )
    )

    assert event.execution_status == "SUCCESS"
    assert event.decision_json["decision"] == "ALLOW"
    assert "payload_sanitization_required" in event.decision_json["triggered_policies"]
    assert any("redacted EMAIL" in r for r in event.decision_json["reasons"])
    # The final ALLOW decision must still report what was found upstream --
    # reasons and sensitive_entities describing the same request must not
    # contradict each other (reasons says "redacted EMAIL" while
    # sensitive_entities used to be left at [] from the clean re-decide pass).
    assert event.decision_json["sensitive_entities"] == ["EMAIL"]


def test_sanitize_loop_gives_up_after_max_attempts(monkeypatch, tmp_path):
    """
    If the detector can never produce a clean pass (pathological case),
    guarded_execution must fail closed to BLOCK rather than loop forever
    or execute an unsanitized payload.
    """
    from app.domains.agent.services import guarded_execution
    from app.domains.guardrail.decision.simple import decide as real_decide

    call_count = {"n": 0}

    def _always_dirty(action):
        call_count["n"] += 1
        from app.core.schemas import Decision as D

        result = real_decide(action)
        # Force every pass to look dirty, regardless of what the real
        # detector found, to simulate a detector that never converges.
        result = result.model_copy(
            update={
                "decision": D.SANITIZE,
                "sensitive_entities": ["EMAIL"],
                "sanitized_payload": action.payload,
            }
        )
        return result

    # guarded_execution imported `decide` by name (`from ... import decide`),
    # so the reference to patch is guarded_execution.decide, not the
    # source module's decide -- patching the source module would not
    # affect the already-bound name inside guarded_execution.
    monkeypatch.setattr(guarded_execution, "decide", _always_dirty)

    event = asyncio.run(
        guarded_execution.run_guarded_action(
            {
                "action_type": "FILE_READ",
                "target_system": "local_file",
                "target": "sample.txt",
                "risk_hint": "file_read",
                "payload": {"note": "foo@example.com"},
            },
            audit=AuditRepository(str(tmp_path / "audit.jsonl")),
        )
    )

    assert event.execution_status == "BLOCKED"
    assert event.decision_json["decision"] == "BLOCK"
    assert "sanitize_loop_limit_exceeded" in event.decision_json["triggered_policies"]
    # Loop bound respected: build_action_request's initial decide() call
    # plus MAX_SANITIZE_LOOPS re-decides.
    assert call_count["n"] <= guarded_execution.MAX_SANITIZE_LOOPS + 1


def test_sanitize_then_need_approval_enqueues_sanitized_payload(monkeypatch, tmp_path):
    """
    A risky action that also carries PII must sanitize first, THEN route
    to NEED_APPROVAL with the already-redacted payload -- not the raw one.
    """
    from app.domains.agent.services import guarded_execution

    captured = {}

    async def _fake_create_pending_approval(request, decision):
        captured["request"] = request
        captured["decision"] = decision
        from app.domains.approval.schemas import PendingApprovalResponse

        return PendingApprovalResponse(
            run_id=request.run_id,
            action_id=request.action_id,
            request_json=request.model_dump(mode="json", exclude={"payload"}),
            decision_json=decision.model_dump(mode="json"),
            pending_since=decision.created_at,
            expires_at=decision.created_at,
        )

    monkeypatch.setattr(
        guarded_execution, "create_pending_approval", _fake_create_pending_approval
    )

    result = asyncio.run(
        guarded_execution.run_guarded_action(
            {
                "action_type": "API_CALL",
                "target_system": "gmail",
                "target": "finance@example.com",
                "risk_hint": "external_send",
                "payload": {
                    "action": "send",
                    "body": "card number 4111111111111111",
                },
            }
        )
    )

    assert captured["decision"].decision == Decision.NEED_APPROVAL
    assert "payload_sanitization_required" in captured["decision"].triggered_policies
    assert "4111111111111111" not in str(captured["request"].payload)
    assert result.decision_json["decision"] == "NEED_APPROVAL"
