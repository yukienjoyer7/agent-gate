"""Endpoint tests for the reactive chat endpoints (execute / stream / respond).

The planner and executor are faked so no real LLM, browser or audit store is
touched. The guardrail stays on its deterministic (rule-only) path because
``GUARDRAIL_LLM_ENABLED`` defaults to false.
"""

import time

from fastapi.testclient import TestClient

from app.core.schemas import AuditEvent, ExecutionStatus
from app.domains.agent.services import agent_loop
from app.domains.agent.services.run_registry import run_registry
from app.main import app

client = TestClient(app)


def _event(run_id: str, action_id: str) -> AuditEvent:
    return AuditEvent(
        run_id=run_id,
        action_id=action_id,
        request_json={},
        decision_json={},
        execution_json={"result_summary": "ok", "data": {"final_url": "https://example.test"}},
        execution_status=ExecutionStatus.SUCCESS,
    )


def _install_browser_fakes(monkeypatch) -> None:
    async def fake_plan(prompt):
        return {
            "plan": [
                {
                    "action_type": "BROWSER_OPEN",
                    "target_system": "browser",
                    "target": "https://example.test",
                    "domain": "browser",
                    "risk_hint": "unknown",
                    "payload": {"url": "https://example.test"},
                }
            ],
            "llm_provider": "dummy",
            "raw_prompt": prompt,
            "human_readable": "",
        }

    async def fake_replan(prompt, context):
        return []

    async def fake_browser(**kwargs):
        return _event(kwargs["run_id"], kwargs["action_id"])

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "parse_next_steps", fake_replan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent", fake_browser)


def _wait_until_done(run_id: str, timeout: float = 3.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = run_registry.get(run_id)
        if run is not None and run.status.value in ("done", "error", "blocked", "declined"):
            return run.status.value
        time.sleep(0.02)
    raise TimeoutError(f"run {run_id} did not finish")


def test_execute_starts_background_run(monkeypatch) -> None:
    _install_browser_fakes(monkeypatch)

    response = client.post("/api/v1/chat/execute", json={"prompt": "open example.test"})
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["status"] == "running"
    assert body["respond_endpoint"].endswith(f"/{body['run_id']}/respond")

    assert _wait_until_done(body["run_id"]) == "done"


def test_stream_execute_emits_sse_events(monkeypatch) -> None:
    _install_browser_fakes(monkeypatch)

    with client.stream(
        "POST", "/api/v1/chat/execute/stream", json={"prompt": "open example.test"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        text = "".join(response.iter_text())

    assert "event: run_started" in text
    assert "event: plan" in text
    assert "event: guardrail" in text
    assert "event: step_result" in text
    assert "event: done" in text
    assert '"run_id"' in text


def test_respond_404_for_unknown_run() -> None:
    response = client.post(
        "/api/v1/chat/execute/run_missing/respond",
        json={"step_index": 0, "action": "approve"},
    )
    assert response.status_code == 404


def test_respond_rejects_fields_for_approve_action(monkeypatch) -> None:
    _install_browser_fakes(monkeypatch)
    run_id = client.post(
        "/api/v1/chat/execute", json={"prompt": "open example.test"}
    ).json()["run_id"]

    response = client.post(
        f"/api/v1/chat/execute/{run_id}/respond",
        json={"step_index": 0, "action": "approve", "fields": {"x": "y"}},
    )
    assert response.status_code == 422
