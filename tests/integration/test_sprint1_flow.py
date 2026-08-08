import asyncio

import pytest

from app.config.settings import get_settings
from app.domains.agent.services import run_guarded_action
from app.domains.audit.repositories import AuditRepository
from app.tracing import TraceWriter


def test_guarded_local_file_flow(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))
    get_settings.cache_clear()

    event = asyncio.run(
        run_guarded_action(
            {
                "action_type": "FILE_READ",
                "target_system": "local_file",
                "target": "sample.txt",
                "risk_hint": "file_read",
                "payload": {"action": "read", "path": "sample.txt"},
            },
            audit=AuditRepository(str(tmp_path / "audit.jsonl")),
            traces=TraceWriter(str(tmp_path / "traces.jsonl")),
        )
    )

    assert event.execution_status == "SUCCESS"
    assert event.request_json["target_system"] == "local_file"
    assert event.execution_json["data"]["content_preview"] == "hello"
    assert event.latency["total_ms"] >= 0


def test_guarded_browser_snapshot_flow(tmp_path):
    """ExecutionRouter routes BROWSER_* actions through the real Playwright
    pipeline (app.executors.browser_executor.BrowserExecutor), not a mock.
    Uses a data: URL so the test stays hermetic (no real network needed)."""
    page_url = "data:text/html,<html><body><button>Continue</button></body></html>"
    event = asyncio.run(
        run_guarded_action(
            {
                "action_type": "BROWSER_SNAPSHOT",
                "target_system": "browser",
                "target": page_url,
                "payload": {"url": page_url},
            },
            audit=AuditRepository(str(tmp_path / "audit.jsonl")),
            traces=TraceWriter(str(tmp_path / "traces.jsonl")),
        )
    )

    error_message = (event.execution_json.get("error") or {}).get("message", "")
    if event.execution_status == "FAILED" and "Executable doesn't exist" in error_message:
        pytest.skip("Playwright browser not installed (run `playwright install chromium`)")

    assert event.execution_status == "SUCCESS"
    assert event.execution_json["data"]["url"] == page_url
    assert isinstance(event.execution_json["data"]["snapshot"], list)
