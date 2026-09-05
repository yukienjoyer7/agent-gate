from __future__ import annotations

import pytest

from app.core.schemas import (
    ActionRequest,
    Decision,
    DecisionResponse,
    ExecutionResult,
    ExecutionStatus,
)
from app.domains.audit.repositories import audit_repository_db
from app.domains.audit.repositories.audit_repository_db import AuditRepositoryDB


class _FakeSession:
    def __init__(self, commit_error: Exception | None = None) -> None:
        self.added = []
        self.commit_error = commit_error
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.commits += 1
        if self.commit_error:
            raise self.commit_error

    async def rollback(self) -> None:
        self.rollbacks += 1


def _audit_inputs():
    request = ActionRequest(
        run_id="run_test",
        action_id="act_test",
        action_type="BROWSER_OPEN",
        target_system="browser",
        target="https://example.test",
    )
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ALLOW,
    )
    execution = ExecutionResult(
        run_id=request.run_id,
        action_id=request.action_id,
        executor="test",
        status=ExecutionStatus.SUCCESS,
    )
    return request, decision, execution


@pytest.mark.asyncio
async def test_audit_write_commits_with_a_fresh_default_session(monkeypatch) -> None:
    sessions: list[_FakeSession] = []

    def fake_session_local() -> _FakeSession:
        fake_session = _FakeSession()
        sessions.append(fake_session)
        return fake_session

    monkeypatch.setattr(audit_repository_db, "SessionLocal", fake_session_local)
    repository = AuditRepositoryDB()
    request, decision, execution = _audit_inputs()

    event = await repository.write(request, decision, execution)
    second_request = request.model_copy(update={"action_id": "act_second"})
    second_decision = decision.model_copy(update={"action_id": second_request.action_id})
    second_execution = execution.model_copy(update={"action_id": second_request.action_id})
    await repository.write(second_request, second_decision, second_execution)

    assert event.action_id == request.action_id
    assert len(sessions) == 2
    assert len(sessions[0].added) == 1
    assert len(sessions[1].added) == 1
    assert sessions[0].commits == 1
    assert sessions[1].commits == 1
    assert sessions[0].rollbacks == 0
    assert sessions[1].rollbacks == 0


@pytest.mark.asyncio
async def test_audit_write_rolls_back_when_commit_fails() -> None:
    failed_session = _FakeSession(commit_error=RuntimeError("commit failed"))
    repository = AuditRepositoryDB(session=failed_session)
    request, decision, execution = _audit_inputs()

    with pytest.raises(RuntimeError, match="commit failed"):
        await repository.write(request, decision, execution)

    assert failed_session.commits == 1
    assert failed_session.rollbacks == 1
