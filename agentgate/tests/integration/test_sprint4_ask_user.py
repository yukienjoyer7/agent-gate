"""
Integration tests for the ASK_USER clarification flow, mirroring
tests/integration/test_sprint3_pending_approvals.py for the pending
_approvals queue. Exercises the real Postgres-backed audit_logs +
pending_user_questions tables (AUDIT_BACKEND=postgres). Each test is
skipped if no Postgres is reachable at DATABASE_URL.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError

from app.config.settings import get_settings
from app.database.models.pending_user_question import PendingUserQuestion
from app.database.session import SessionLocal
from app.main import app


def _require_postgres() -> None:
    async def _ping() -> None:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))

    try:
        asyncio.run(_ping())
    except OperationalError:
        pytest.skip("no reachable Postgres at DATABASE_URL; skipping DB-backed ask-user tests")


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_BACKEND", "postgres")
    monkeypatch.setenv("ASK_USER_TTL_MINUTES", "10")
    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello from ask-user flow", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))
    get_settings.cache_clear()
    _require_postgres()
    return TestClient(app)


def _low_confidence_proposal(confidence: float = 0.3) -> dict:
    return {
        "action_type": "API_CALL",
        "target_system": "local_file",
        "target": "sample.txt",
        "risk_hint": "file_read",
        "confidence": confidence,
        "payload": {"action": "read", "path": "sample.txt"},
    }


def test_ask_user_action_is_queued_not_audited(client):
    resp = client.post("/api/v1/actions/run", json=_low_confidence_proposal())
    assert resp.status_code == 200
    body = resp.json()
    assert body["execution_status"] == "PENDING_USER_INPUT"
    assert body["clarifying_question"]

    # Nothing in audit_logs yet -- only the pending queue knows about it.
    assert client.get(f"/api/v1/actions/{body['action_id']}").status_code == 404
    queue_ids = [a["action_id"] for a in client.get("/api/v1/ask-user").json()]
    assert body["action_id"] in queue_ids


def test_cancel_writes_exactly_one_audit_row(client):
    action_id = client.post("/api/v1/actions/run", json=_low_confidence_proposal()).json()["action_id"]

    respond = client.post(f"/api/v1/ask-user/{action_id}/respond", json={"proceed": False})
    assert respond.status_code == 200
    assert respond.json()["execution_status"] == "CANCELLED"

    assert action_id not in [a["action_id"] for a in client.get("/api/v1/ask-user").json()]
    assert client.get(f"/api/v1/actions/{action_id}").json()["execution_status"] == "CANCELLED"


def test_second_respond_call_gets_404(client):
    action_id = client.post("/api/v1/actions/run", json=_low_confidence_proposal()).json()["action_id"]

    first = client.post(f"/api/v1/ask-user/{action_id}/respond", json={"proceed": True})
    assert first.status_code == 200

    second = client.post(f"/api/v1/ask-user/{action_id}/respond", json={"proceed": True})
    assert second.status_code == 404


def test_proceed_actually_executes_the_action(client):
    action_id = client.post("/api/v1/actions/run", json=_low_confidence_proposal()).json()["action_id"]

    respond = client.post(f"/api/v1/ask-user/{action_id}/respond", json={"proceed": True})
    assert respond.status_code == 200
    body = respond.json()
    assert body["execution_status"] == "SUCCESS"
    assert body["execution_json"]["executor"] == "local_file"
    assert "hello from ask-user flow" in body["execution_json"]["data"]["content_preview"]
    assert "confirmed by user" in body["decision_json"]["reasons"]


def test_payload_updates_are_merged_before_execution(client):
    proposal = _low_confidence_proposal()
    proposal["payload"] = {"action": "read", "path": "WRONG.txt"}
    action_id = client.post("/api/v1/actions/run", json=proposal).json()["action_id"]

    respond = client.post(
        f"/api/v1/ask-user/{action_id}/respond",
        json={"proceed": True, "payload_updates": {"path": "sample.txt"}},
    )
    assert respond.status_code == 200
    body = respond.json()
    assert body["execution_status"] == "SUCCESS"
    assert "hello from ask-user flow" in body["execution_json"]["data"]["content_preview"]


def test_proceed_with_sensitive_correction_still_sanitizes(client):
    action_id = client.post("/api/v1/actions/run", json=_low_confidence_proposal()).json()["action_id"]

    respond = client.post(
        f"/api/v1/ask-user/{action_id}/respond",
        json={"proceed": True, "payload_updates": {"note": "contact me at foo@example.com"}},
    )
    assert respond.status_code == 200
    body = respond.json()
    assert body["execution_status"] == "SUCCESS"
    assert "payload_sanitization_required" in body["decision_json"]["triggered_policies"]
    assert "EMAIL" in body["decision_json"]["sensitive_entities"]


def test_proceed_with_still_risky_correction_escalates_to_approval(client):
    proposal = _low_confidence_proposal()
    proposal["risk_hint"] = "external_send"
    action_id = client.post("/api/v1/actions/run", json=proposal).json()["action_id"]

    respond = client.post(f"/api/v1/ask-user/{action_id}/respond", json={"proceed": True})
    assert respond.status_code == 200
    body = respond.json()

    # Still nothing in audit_logs -- handed off to the reviewer queue instead.
    assert client.get(f"/api/v1/actions/{action_id}").status_code == 404
    assert action_id in [a["action_id"] for a in client.get("/api/v1/approvals").json()]
    assert body["decision_json"]["decision"] == "NEED_APPROVAL"


def test_expired_question_resolves_to_expired_and_returns_410(client):
    action_id = client.post("/api/v1/actions/run", json=_low_confidence_proposal()).json()["action_id"]

    async def force_expire() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(PendingUserQuestion)
                .where(PendingUserQuestion.action_id == action_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

    asyncio.run(force_expire())

    respond = client.post(f"/api/v1/ask-user/{action_id}/respond", json={"proceed": True})
    assert respond.status_code == 410
    assert client.get(f"/api/v1/actions/{action_id}").json()["execution_status"] == "EXPIRED"


def test_listing_ask_user_lazily_sweeps_expired_rows(client):
    action_id = client.post("/api/v1/actions/run", json=_low_confidence_proposal()).json()["action_id"]

    async def force_expire() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(PendingUserQuestion)
                .where(PendingUserQuestion.action_id == action_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

    asyncio.run(force_expire())

    assert action_id not in [a["action_id"] for a in client.get("/api/v1/ask-user").json()]
    assert client.get(f"/api/v1/actions/{action_id}").json()["execution_status"] == "EXPIRED"
