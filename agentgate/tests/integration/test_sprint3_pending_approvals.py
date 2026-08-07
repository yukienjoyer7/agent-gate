"""
Integration tests for the pending-approval flow in
pending-approval-design.md. Unlike the other Sprint 2 integration tests,
this exercises the real Postgres-backed audit_logs + pending_approvals
tables (AUDIT_BACKEND=postgres) rather than the JSONL fallback, since the
whole point of the design is the interaction between the two Postgres
tables (immutability trigger, action_id uniqueness, atomic claim). Each
test is skipped if no Postgres is reachable at DATABASE_URL.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError

from app.config.settings import get_settings
from app.database.models.pending_approval import PendingApproval
from app.database.session import SessionLocal
from app.main import app


def _require_postgres() -> None:
    async def _ping() -> None:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))

    try:
        asyncio.run(_ping())
    except OperationalError:
        pytest.skip("no reachable Postgres at DATABASE_URL; skipping DB-backed approval tests")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("AUDIT_BACKEND", "postgres")
    monkeypatch.setenv("APPROVAL_TTL_MINUTES", "30")
    get_settings.cache_clear()
    _require_postgres()
    return TestClient(app)


def _risky_gmail_proposal() -> dict:
    return {
        "action_type": "API_CALL",
        "target_system": "gmail",
        "target": "someone@example.com",
        "risk_hint": "external_send",
        "payload": {"action": "send", "to": "someone@example.com", "body": "hi"},
    }


def test_need_approval_action_is_queued_not_audited(client):
    resp = client.post("/api/v1/actions/run", json=_risky_gmail_proposal())
    assert resp.status_code == 200
    body = resp.json()
    assert body["execution_status"] == "PENDING_APPROVAL"

    # Nothing in audit_logs yet -- only the pending queue knows about it.
    assert client.get(f"/api/v1/actions/{body['action_id']}").status_code == 404
    queue_ids = [a["action_id"] for a in client.get("/api/v1/approvals").json()]
    assert body["action_id"] in queue_ids


def test_reject_writes_exactly_one_audit_row(client):
    action_id = client.post("/api/v1/actions/run", json=_risky_gmail_proposal()).json()["action_id"]

    decide = client.post(f"/api/v1/approvals/{action_id}/decide", json={"decision": "REJECT"})
    assert decide.status_code == 200
    assert decide.json()["execution_status"] == "REJECTED"
    assert decide.json()["decision_json"]["reviewer_decision"] == "REJECTED"
    assert decide.json()["decision_json"]["pending_duration_ms"] >= 0

    # Resolved -- gone from the queue, exactly one row in audit_logs.
    assert action_id not in [a["action_id"] for a in client.get("/api/v1/approvals").json()]
    assert client.get(f"/api/v1/actions/{action_id}").json()["execution_status"] == "REJECTED"


def test_second_decide_call_gets_404(client):
    action_id = client.post("/api/v1/actions/run", json=_risky_gmail_proposal()).json()["action_id"]

    first = client.post(f"/api/v1/approvals/{action_id}/decide", json={"decision": "APPROVE"})
    assert first.status_code == 200

    second = client.post(f"/api/v1/approvals/{action_id}/decide", json={"decision": "APPROVE"})
    assert second.status_code == 404


def test_approve_actually_executes_the_action(client, tmp_path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello from approval flow", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))
    get_settings.cache_clear()

    action_id = client.post(
        "/api/v1/actions/run",
        json={
            "action_type": "API_CALL",
            "target_system": "local_file",
            "target": "sample.txt",
            "risk_hint": "destructive",
            "payload": {"action": "read", "path": "sample.txt"},
        },
    ).json()["action_id"]

    decide = client.post(f"/api/v1/approvals/{action_id}/decide", json={"decision": "APPROVE"})
    assert decide.status_code == 200
    body = decide.json()
    assert body["execution_status"] == "SUCCESS"
    # This is the real executor result, not the router's NEED_APPROVAL stub.
    assert body["execution_json"]["executor"] == "local_file"
    assert "hello from approval flow" in body["execution_json"]["data"]["content_preview"]


def test_expired_approval_resolves_to_expired_and_returns_410(client):
    action_id = client.post("/api/v1/actions/run", json=_risky_gmail_proposal()).json()["action_id"]

    async def force_expire() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(PendingApproval)
                .where(PendingApproval.action_id == action_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

    asyncio.run(force_expire())

    decide = client.post(f"/api/v1/approvals/{action_id}/decide", json={"decision": "APPROVE"})
    assert decide.status_code == 410
    assert client.get(f"/api/v1/actions/{action_id}").json()["execution_status"] == "EXPIRED"


def test_listing_approvals_lazily_sweeps_expired_rows(client):
    action_id = client.post("/api/v1/actions/run", json=_risky_gmail_proposal()).json()["action_id"]

    async def force_expire() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(PendingApproval)
                .where(PendingApproval.action_id == action_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

    asyncio.run(force_expire())

    # GET /approvals should never show it as pending, and by the time it
    # returns, the sweep has already written the EXPIRED row.
    assert action_id not in [a["action_id"] for a in client.get("/api/v1/approvals").json()]
    assert client.get(f"/api/v1/actions/{action_id}").json()["execution_status"] == "EXPIRED"
