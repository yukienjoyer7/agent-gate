"""
DB-free unit tests for clarification_service's resolution logic. Uses a
fake repository backed by an in-memory dict (same shape contract as
PendingUserQuestionRepository: create/list/get/claim) plus the real JSONL
AuditRepository, so this exercises _resolve()'s merge/re-decide/escalate
branches without needing Postgres -- unlike
tests/integration/test_sprint4_ask_user.py, which exercises the same
flow end-to-end through the real HTTP + DB stack and is skipped when no
Postgres is reachable.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from app.config.settings import get_settings
from app.core.schemas import ActionRequest, Decision, DecisionResponse
from app.database.models.pending_user_question import PendingUserQuestion
from app.domains.audit.repositories import AuditRepository
from app.domains.clarification.schemas import UserResponseRequest
from app.domains.clarification.services import clarification_service


class _CountingAudit:
    """
    Wraps a real AuditRepository and counts write() calls, so tests can
    assert the intermediate-vs-terminal invariant directly (write() called
    zero times for still_pending/escalated_to_approval, exactly once for
    resolved) instead of just inferring it from audit.list().
    """

    def __init__(self, path: str) -> None:
        self._inner = AuditRepository(path)
        self.write_calls = 0

    async def write(self, *args, **kwargs):
        self.write_calls += 1
        return await self._inner.write(*args, **kwargs)

    async def list(self):
        return await self._inner.list()


class _FakeRepo:
    def __init__(self) -> None:
        self._rows: dict[str, PendingUserQuestion] = {}

    def seed(
        self,
        request: ActionRequest,
        decision: DecisionResponse,
        ttl_minutes: int = 10,
        clarification_round: int = 1,
    ) -> None:
        now = datetime.now(UTC)
        self._rows[request.action_id] = PendingUserQuestion(
            action_id=request.action_id,
            run_id=request.run_id,
            question=decision.clarifying_question or "proceed?",
            request_json={**request.model_dump(mode="json"), "payload": request.payload},
            decision_json=decision.model_dump(mode="json"),
            clarification_round=clarification_round,
            pending_since=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
        )

    async def create(
        self,
        request: ActionRequest,
        decision: DecisionResponse,
        ttl_minutes: int = 10,
        clarification_round: int = 1,
    ) -> PendingUserQuestion:
        self.seed(request, decision, ttl_minutes=ttl_minutes, clarification_round=clarification_round)
        return self._rows[request.action_id]

    async def claim(self, action_id: str):
        return self._rows.pop(action_id, None)

    async def list(self):
        return list(self._rows.values())


def _low_confidence_request(**overrides) -> ActionRequest:
    fields = dict(
        action_type="FILE_READ",
        target_system="local_file",
        target="sample.txt",
        risk_hint="file_read",
        confidence=0.3,
        payload={"action": "read", "path": "sample.txt"},
    )
    fields.update(overrides)
    return ActionRequest(**fields)


def test_proceed_false_cancels_without_executing(tmp_path):
    request = _low_confidence_request()
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ASK_USER,
        clarifying_question="proceed?",
    )
    repo = _FakeRepo()
    repo.seed(request, decision)
    audit = AuditRepository(str(tmp_path / "audit.jsonl"))

    outcome, event = asyncio.run(
        clarification_service.decide_pending_question(
            request.action_id, UserResponseRequest(proceed=False), repo=repo, audit=audit
        )
    )

    assert outcome == "resolved"
    assert event.execution_status == "CANCELLED"
    assert "cancelled by user" in event.decision_json["reasons"]
    assert await_list_empty(repo)


def test_proceed_true_reexecutes_with_merged_payload(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))
    get_settings.cache_clear()

    request = _low_confidence_request(payload={"action": "read", "path": "WRONG.txt"})
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ASK_USER,
        clarifying_question="which file?",
    )
    repo = _FakeRepo()
    repo.seed(request, decision)
    audit = AuditRepository(str(tmp_path / "audit.jsonl"))

    outcome, event = asyncio.run(
        clarification_service.decide_pending_question(
            request.action_id,
            UserResponseRequest(proceed=True, payload_updates={"path": "sample.txt"}),
            repo=repo,
            audit=audit,
        )
    )

    assert outcome == "resolved"
    assert event.execution_status == "SUCCESS"
    assert "hello" in event.execution_json["data"]["content_preview"]
    assert "confirmed by user" in event.decision_json["reasons"]


def test_proceed_true_with_pii_correction_still_sanitizes(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))
    get_settings.cache_clear()

    request = _low_confidence_request()
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ASK_USER,
        clarifying_question="proceed?",
    )
    repo = _FakeRepo()
    repo.seed(request, decision)
    audit = AuditRepository(str(tmp_path / "audit.jsonl"))

    outcome, event = asyncio.run(
        clarification_service.decide_pending_question(
            request.action_id,
            UserResponseRequest(proceed=True, payload_updates={"note": "foo@example.com"}),
            repo=repo,
            audit=audit,
        )
    )

    assert outcome == "resolved"
    assert event.execution_status == "SUCCESS"
    assert "EMAIL" in event.decision_json["sensitive_entities"]
    assert "payload_sanitization_required" in event.decision_json["triggered_policies"]


def test_proceed_true_with_risky_correction_escalates_to_approval(tmp_path):
    request = _low_confidence_request(risk_hint="external_send")
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ASK_USER,
        clarifying_question="proceed?",
    )
    repo = _FakeRepo()
    repo.seed(request, decision)
    audit = AuditRepository(str(tmp_path / "audit.jsonl"))

    async def _fake_create_pending_approval(req, dec):
        from app.domains.approval.schemas import PendingApprovalResponse

        return PendingApprovalResponse(
            run_id=req.run_id,
            action_id=req.action_id,
            request_json=req.model_dump(mode="json", exclude={"payload"}),
            decision_json=dec.model_dump(mode="json"),
            pending_since=dec.created_at,
            expires_at=dec.created_at,
        )

    orig = clarification_service.create_pending_approval
    clarification_service.create_pending_approval = _fake_create_pending_approval
    try:
        outcome, result = asyncio.run(
            clarification_service.decide_pending_question(
                request.action_id,
                UserResponseRequest(proceed=True, payload_updates={"recipient": "user@example.com"}),
                repo=repo,
                audit=audit,
            )
        )
    finally:
        clarification_service.create_pending_approval = orig

    assert outcome == "escalated_to_approval"
    assert result.decision_json["decision"] == "NEED_APPROVAL"


def test_proceed_true_with_nested_correction_preserves_sibling_keys(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))
    get_settings.cache_clear()

    request = _low_confidence_request(
        payload={"action": "read", "path": "sample.txt", "meta": {"owner": "alice", "team": "infra"}}
    )
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ASK_USER,
        clarifying_question="who requested this?",
    )
    repo = _FakeRepo()
    repo.seed(request, decision)
    audit = AuditRepository(str(tmp_path / "audit.jsonl"))

    # ActionRequest.payload is excluded from audit's request_json (see
    # AuditRepository.write), so the merge is verified by spying on the
    # request actually passed into decide() rather than reading it back
    # out of the audit event.
    captured_payloads = []
    orig_decide = clarification_service.decide

    def _spy_decide(req):
        captured_payloads.append(req.payload)
        return orig_decide(req)

    monkeypatch.setattr(clarification_service, "decide", _spy_decide)

    outcome, event = asyncio.run(
        clarification_service.decide_pending_question(
            request.action_id,
            UserResponseRequest(proceed=True, payload_updates={"meta": {"owner": "bob"}}),
            repo=repo,
            audit=audit,
        )
    )

    assert outcome == "resolved"
    assert event.execution_status == "SUCCESS"
    assert captured_payloads[0]["meta"] == {"owner": "bob", "team": "infra"}


def test_unknown_action_id_returns_none(tmp_path):
    repo = _FakeRepo()
    audit = AuditRepository(str(tmp_path / "audit.jsonl"))

    result = asyncio.run(
        clarification_service.decide_pending_question(
            "act_does_not_exist", UserResponseRequest(proceed=False), repo=repo, audit=audit
        )
    )

    assert result is None


def test_proceed_true_without_updates_goes_back_for_another_round(tmp_path):
    # proceed=True with no payload_updates doesn't change the proposal --
    # the required "path" field is still missing, so decide() legitimately
    # returns ASK_USER again (nothing about the proposal changed). That
    # should land back in the queue for another round, not silently
    # execute or block.
    request = _low_confidence_request(payload={"action": "read"})
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ASK_USER,
        clarifying_question="which file?",
    )
    repo = _FakeRepo()
    repo.seed(request, decision, clarification_round=1)
    audit = AuditRepository(str(tmp_path / "audit.jsonl"))

    outcome, pending = asyncio.run(
        clarification_service.decide_pending_question(
            request.action_id, UserResponseRequest(proceed=True), repo=repo, audit=audit
        )
    )

    assert outcome == "still_pending"
    assert pending.clarification_round == 2
    # unaudited: the row moved, nothing was written to the audit log
    assert repo._rows[request.action_id].clarification_round == 2
    assert asyncio.run(audit.list()) == []


def test_clarification_round_limit_fails_closed_to_block(tmp_path):
    request = _low_confidence_request(payload={"action": "read"})
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ASK_USER,
        clarifying_question="which file?",
    )
    repo = _FakeRepo()
    repo.seed(request, decision, clarification_round=clarification_service.MAX_CLARIFICATION_ROUNDS)
    audit = AuditRepository(str(tmp_path / "audit.jsonl"))

    outcome, event = asyncio.run(
        clarification_service.decide_pending_question(
            request.action_id, UserResponseRequest(proceed=True), repo=repo, audit=audit
        )
    )

    assert outcome == "resolved"
    assert event.decision_json["decision"] == "BLOCK"
    assert "clarification round limit" in " ".join(event.decision_json["reasons"])
    assert await_list_empty(repo)


def await_list_empty(repo: _FakeRepo) -> bool:
    return len(repo._rows) == 0


def test_still_pending_never_calls_audit_write(tmp_path):
    request = _low_confidence_request(payload={"action": "read"})
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ASK_USER,
        clarifying_question="which file?",
    )
    repo = _FakeRepo()
    repo.seed(request, decision, clarification_round=1)
    audit = _CountingAudit(str(tmp_path / "audit.jsonl"))

    outcome, pending = asyncio.run(
        clarification_service.decide_pending_question(
            request.action_id, UserResponseRequest(proceed=True), repo=repo, audit=audit
        )
    )

    assert outcome == "still_pending"
    assert audit.write_calls == 0


def test_escalated_to_approval_never_calls_audit_write(tmp_path):
    request = _low_confidence_request(risk_hint="external_send")
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ASK_USER,
        clarifying_question="proceed?",
    )
    repo = _FakeRepo()
    repo.seed(request, decision)
    audit = _CountingAudit(str(tmp_path / "audit.jsonl"))

    async def _fake_create_pending_approval(req, dec):
        from app.domains.approval.schemas import PendingApprovalResponse

        return PendingApprovalResponse(
            run_id=req.run_id,
            action_id=req.action_id,
            request_json=req.model_dump(mode="json", exclude={"payload"}),
            decision_json=dec.model_dump(mode="json"),
            pending_since=dec.created_at,
            expires_at=dec.created_at,
        )

    orig = clarification_service.create_pending_approval
    clarification_service.create_pending_approval = _fake_create_pending_approval
    try:
        outcome, _ = asyncio.run(
            clarification_service.decide_pending_question(
                request.action_id,
                UserResponseRequest(proceed=True, payload_updates={"recipient": "user@example.com"}),
                repo=repo,
                audit=audit,
            )
        )
    finally:
        clarification_service.create_pending_approval = orig

    assert outcome == "escalated_to_approval"
    assert audit.write_calls == 0


def test_terminal_outcomes_write_audit_exactly_once(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))
    get_settings.cache_clear()
    audit = _CountingAudit(str(tmp_path / "audit.jsonl"))

    # cancelled
    request = _low_confidence_request()
    decision = DecisionResponse(
        run_id=request.run_id, action_id=request.action_id, decision=Decision.ASK_USER
    )
    repo = _FakeRepo()
    repo.seed(request, decision)
    outcome, _ = asyncio.run(
        clarification_service.decide_pending_question(
            request.action_id, UserResponseRequest(proceed=False), repo=repo, audit=audit
        )
    )
    assert outcome == "resolved"
    assert audit.write_calls == 1

    # executed
    request = _low_confidence_request(payload={"action": "read", "path": "WRONG.txt"})
    decision = DecisionResponse(
        run_id=request.run_id, action_id=request.action_id, decision=Decision.ASK_USER
    )
    repo = _FakeRepo()
    repo.seed(request, decision)
    outcome, _ = asyncio.run(
        clarification_service.decide_pending_question(
            request.action_id,
            UserResponseRequest(proceed=True, payload_updates={"path": "sample.txt"}),
            repo=repo,
            audit=audit,
        )
    )
    assert outcome == "resolved"
    assert audit.write_calls == 2

    # round-limit -> BLOCK
    request = _low_confidence_request(payload={"action": "read"})
    decision = DecisionResponse(
        run_id=request.run_id, action_id=request.action_id, decision=Decision.ASK_USER
    )
    repo = _FakeRepo()
    repo.seed(request, decision, clarification_round=clarification_service.MAX_CLARIFICATION_ROUNDS)
    outcome, _ = asyncio.run(
        clarification_service.decide_pending_question(
            request.action_id, UserResponseRequest(proceed=True), repo=repo, audit=audit
        )
    )
    assert outcome == "resolved"
    assert audit.write_calls == 3

    # expired
    request = _low_confidence_request()
    decision = DecisionResponse(
        run_id=request.run_id, action_id=request.action_id, decision=Decision.ASK_USER
    )
    repo = _FakeRepo()
    repo.seed(request, decision, ttl_minutes=-1)
    outcome, _ = asyncio.run(
        clarification_service.decide_pending_question(
            request.action_id, UserResponseRequest(proceed=True), repo=repo, audit=audit
        )
    )
    assert outcome == "expired"
    assert audit.write_calls == 4
