import asyncio

from app.core.schemas import Decision


def _low_confidence_proposal(confidence: float = 0.3, complete: bool = False) -> dict:
    # FILE_READ is registered in clarification/rules.py as requiring
    # "path" -- ASK_USER for it is now driven by completeness, not
    # confidence, so this omits "path" by default. Pass complete=True to
    # get a fully-specified proposal instead.
    payload = {"action": "read"}
    if complete:
        payload["path"] = "sample.txt"
    return {
        "action_type": "FILE_READ",
        "target_system": "local_file",
        "target": "sample.txt",
        "risk_hint": "file_read",
        "confidence": confidence,
        "payload": payload,
    }


def test_low_confidence_action_is_queued_not_executed(monkeypatch):
    """
    A low-confidence action must go to the ask-user queue -- no audit row,
    no execution -- and the caller gets back a PendingUserQuestionResponse
    carrying the clarifying question.
    """
    from app.domains.agent.services import guarded_execution
    from app.domains.clarification.schemas import PendingUserQuestionResponse

    captured = {}

    async def _fake_create_pending_question(request, decision):
        captured["request"] = request
        captured["decision"] = decision
        return PendingUserQuestionResponse(
            run_id=request.run_id,
            action_id=request.action_id,
            clarifying_question=decision.clarifying_question,
            request_json=request.model_dump(mode="json"),
            decision_json=decision.model_dump(mode="json"),
            pending_since=decision.created_at,
            expires_at=decision.created_at,
        )

    monkeypatch.setattr(
        guarded_execution, "create_pending_question", _fake_create_pending_question
    )

    result = asyncio.run(guarded_execution.run_guarded_action(_low_confidence_proposal()))

    assert captured["decision"].decision == Decision.ASK_USER
    assert captured["decision"].clarifying_question is not None
    assert result.execution_status.value == "PENDING_USER_INPUT"
    assert result.clarifying_question == captured["decision"].clarifying_question


def test_sanitize_then_ask_user_enqueues_redacted_payload(monkeypatch):
    """
    A low-confidence action that also carries PII must sanitize first,
    THEN route to ASK_USER with the already-redacted payload -- the
    clarification queue should never see the raw email.
    """
    from app.domains.agent.services import guarded_execution
    from app.domains.clarification.schemas import PendingUserQuestionResponse

    captured = {}

    async def _fake_create_pending_question(request, decision):
        captured["request"] = request
        captured["decision"] = decision
        return PendingUserQuestionResponse(
            run_id=request.run_id,
            action_id=request.action_id,
            clarifying_question=decision.clarifying_question,
            request_json=request.model_dump(mode="json"),
            decision_json=decision.model_dump(mode="json"),
            pending_since=decision.created_at,
            expires_at=decision.created_at,
        )

    monkeypatch.setattr(
        guarded_execution, "create_pending_question", _fake_create_pending_question
    )

    proposal = _low_confidence_proposal(confidence=0.2)
    proposal["payload"]["note"] = "contact me at foo@example.com"

    asyncio.run(guarded_execution.run_guarded_action(proposal))

    assert captured["decision"].decision == Decision.ASK_USER
    assert "payload_sanitization_required" in captured["decision"].triggered_policies
    assert "foo@example.com" not in str(captured["request"].payload)


def test_high_confidence_action_never_reaches_ask_user_queue(monkeypatch, tmp_path):
    from app.domains.agent.services import guarded_execution
    from app.domains.audit.repositories import AuditRepository

    called = {"n": 0}

    async def _should_not_be_called(request, decision):
        called["n"] += 1
        raise AssertionError("create_pending_question should not be called")

    monkeypatch.setattr(
        guarded_execution, "create_pending_question", _should_not_be_called
    )

    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))

    event = asyncio.run(
        guarded_execution.run_guarded_action(
            _low_confidence_proposal(confidence=0.95, complete=True),
            audit=AuditRepository(str(tmp_path / "audit.jsonl")),
        )
    )

    assert called["n"] == 0
    assert event.execution_status == "SUCCESS"


def test_initial_ask_user_never_calls_audit_write(monkeypatch, tmp_path):
    """
    Locks down the intermediate-vs-terminal invariant at the entry point,
    not just inside clarification_service: the very first ASK_USER routing
    (before any user response) must never touch the audit repository.
    """
    from app.domains.agent.services import guarded_execution
    from app.domains.audit.repositories import AuditRepository
    from app.domains.clarification.schemas import PendingUserQuestionResponse

    async def _fake_create_pending_question(request, decision):
        return PendingUserQuestionResponse(
            run_id=request.run_id,
            action_id=request.action_id,
            clarifying_question=decision.clarifying_question,
            request_json=request.model_dump(mode="json"),
            decision_json=decision.model_dump(mode="json"),
            pending_since=decision.created_at,
            expires_at=decision.created_at,
        )

    monkeypatch.setattr(
        guarded_execution, "create_pending_question", _fake_create_pending_question
    )

    class _CountingAudit(AuditRepository):
        def __init__(self, path: str) -> None:
            super().__init__(path)
            self.write_calls = 0

        async def write(self, *args, **kwargs):
            self.write_calls += 1
            return await super().write(*args, **kwargs)

    audit = _CountingAudit(str(tmp_path / "audit.jsonl"))
    result = asyncio.run(
        guarded_execution.run_guarded_action(_low_confidence_proposal(), audit=audit)
    )

    assert result.execution_status.value == "PENDING_USER_INPUT"
    assert audit.write_calls == 0
