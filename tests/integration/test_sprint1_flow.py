import asyncio

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
    event = asyncio.run(
        run_guarded_action(
            {
                "action_type": "BROWSER_SNAPSHOT",
                "target_system": "browser",
                "target": "https://example.test/demo",
                "payload": {"url": "https://example.test/demo"},
            },
            audit=AuditRepository(str(tmp_path / "audit.jsonl")),
            traces=TraceWriter(str(tmp_path / "traces.jsonl")),
        )
    )

    assert event.execution_status == "SUCCESS"
    assert event.execution_json["data"]["snapshot_id"].startswith("snap_")
