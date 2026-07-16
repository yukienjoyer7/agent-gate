import asyncio

from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.domains.agent.services import run_guarded_action
from app.main import app


def test_sprint2_read_endpoints(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "traces.jsonl"))
    get_settings.cache_clear()

    event = asyncio.run(
        run_guarded_action(
            {
                "user_goal": "read demo file",
                "action_type": "FILE_READ",
                "target_system": "local_file",
                "target": "sample.txt",
                "risk_hint": "file_read",
                "payload": {"action": "read", "path": "sample.txt"},
            }
        )
    )

    client = TestClient(app)
    assert client.get("/api/v1/runs").json()[0]["run_id"] == event.run_id
    assert client.get(f"/api/v1/runs/{event.run_id}/actions").json()[0]["action_id"]
    assert client.get(f"/api/v1/actions/{event.action_id}").json()["run_id"] == event.run_id
    assert client.get("/api/v1/audits/latest").json()["action_id"] == event.action_id
    assert client.get("/api/v1/benchmark").json()["action_count"] == 1
