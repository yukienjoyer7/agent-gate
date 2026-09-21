from fastapi.testclient import TestClient

from app.api.audit_scope import event_belongs_to_owner
from app.api.session_context import OwnerContext
from app.core.audit_schema import AuditEvent
from app.core.run_schema import StepStatus
from app.core.schemas import ExecutionStatus
from app.domains.agent.services.run_registry import RunRegistry, StepState, run_registry
from app.main import app


def test_audit_events_are_visible_only_to_their_session() -> None:
    event_a = AuditEvent(
        run_id="run_a",
        action_id="act_a",
        request_json={"owner_id": "session-a", "session_id": "session-a"},
        decision_json={},
        execution_json={},
        execution_status=ExecutionStatus.SUCCESS,
    )
    event_legacy = AuditEvent(
        run_id="run_legacy",
        action_id="act_legacy",
        request_json={},
        decision_json={},
        execution_json={},
        execution_status=ExecutionStatus.SUCCESS,
    )

    assert event_belongs_to_owner(event_a, OwnerContext("session-a", "session-a"))
    assert not event_belongs_to_owner(event_a, OwnerContext("session-b", "session-b"))
    assert not event_belongs_to_owner(event_a, OwnerContext("session-a"))
    assert event_belongs_to_owner(event_legacy, OwnerContext("default"))


def test_removing_an_owner_clears_only_its_ephemeral_runs() -> None:
    registry = RunRegistry()
    owner_run = registry.create("private", metadata={"owner_id": "session-a"})
    other_run = registry.create("other", metadata={"owner_id": "session-b"})

    removed = registry.remove_by_owner_id("session-a")

    assert [run.run_id for run in removed] == [owner_run.run_id]
    assert registry.get(owner_run.run_id) is None
    assert registry.get(other_run.run_id) is other_run
    assert owner_run.events.get_nowait()["data"]["status"] == "cancelled"


def test_chat_run_state_and_respond_are_scoped_to_session(monkeypatch) -> None:
    from app.api import session_context

    async def active_session(session_id: str) -> bool:
        return session_id in {"session-a", "session-b"}

    monkeypatch.setattr(session_context, "touch_browser_session", active_session)
    run = run_registry.create(
        "private run",
        metadata={"owner_id": "session-a", "session_id": "session-a"},
    )
    run.steps.append(
        StepState(
            index=0,
            data={"action_type": "API_CALL", "target_system": "telegram"},
            action_id="act_session_scope",
            status=StepStatus.WAITING_APPROVAL,
        )
    )
    client = TestClient(app)
    try:
        state_a = client.get(
            f"/api/v1/chat/execute/{run.run_id}",
            headers={"X-AgentGate-Session": "session-a"},
        )
        state_b = client.get(
            f"/api/v1/chat/execute/{run.run_id}",
            headers={"X-AgentGate-Session": "session-b"},
        )
        respond_b = client.post(
            f"/api/v1/chat/execute/{run.run_id}/respond",
            headers={"X-AgentGate-Session": "session-b"},
            json={"step_index": 0, "action": "approve"},
        )
        respond_a = client.post(
            f"/api/v1/chat/execute/{run.run_id}/respond",
            headers={"X-AgentGate-Session": "session-a"},
            json={"step_index": 0, "action": "approve"},
        )
        spoofed_owner = client.get(
            f"/api/v1/chat/execute/{run.run_id}",
            headers={"X-AgentGate-Owner": "a" * 43},
        )

        assert state_a.status_code == 200
        assert state_b.status_code == 404
        assert respond_b.status_code == 404
        assert respond_a.status_code == 200
        assert spoofed_owner.status_code == 400
    finally:
        run_registry.remove_by_owner_id("session-a")


def test_session_lifecycle_routes_record_create_and_end(monkeypatch) -> None:
    from app.api.v1 import sessions

    session_id = "a" * 43
    ended: list[tuple[str, str]] = []

    async def create() -> str:
        return session_id

    async def end(value: str, reason: str) -> bool:
        ended.append((value, reason))
        return True

    monkeypatch.setattr(sessions, "create_browser_session", create)
    monkeypatch.setattr(sessions, "end_browser_session", end)
    client = TestClient(app)

    created = client.post("/api/v1/sessions")
    closed = client.post(
        "/api/v1/sessions/end",
        content=f'{{"session_id":"{session_id}","reason":"reset"}}',
        headers={"Content-Type": "text/plain"},
    )

    assert created.status_code == 200
    assert created.json()["session_id"] == session_id
    assert closed.status_code == 200
    assert ended == [(session_id, "reset")]
