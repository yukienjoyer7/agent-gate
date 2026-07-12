import asyncio

from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.domains.agent.services import run_guarded_action
from app.domains.connector.github.github import GitHubConnector
from app.main import app


def test_sprint2_read_endpoints(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("AUDIT_BACKEND", "jsonl")
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


def test_run_action_endpoint_supports_github_repo_metadata(tmp_path, monkeypatch):
    async def fake_get(self, path: str) -> dict:
        assert path == "/repos/octo/demo"
        return {
            "id": 1,
            "full_name": "octo/demo",
            "private": False,
            "default_branch": "main",
            "html_url": "https://github.com/octo/demo",
        }

    monkeypatch.setenv("AUDIT_BACKEND", "jsonl")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "traces.jsonl"))
    monkeypatch.setattr(GitHubConnector, "_get", fake_get)
    get_settings.cache_clear()

    response = TestClient(app).post(
        "/api/v1/actions/run",
        json={
            "user_goal": "inspect repo metadata",
            "action_type": "API_CALL",
            "target_system": "github",
            "target": "octo/demo",
            "payload": {
                "action": "repo_metadata",
                "owner": "octo",
                "repo": "demo",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_status"] == "SUCCESS"
    assert body["execution_json"]["data"]["full_name"] == "octo/demo"
