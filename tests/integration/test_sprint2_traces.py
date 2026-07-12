import asyncio

from app.config.settings import get_settings
from app.domains.agent.services import run_guarded_action
from app.tracing import TraceWriter


def test_guarded_action_writes_model_ready_trace(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "sample.txt").write_text("hello", encoding="utf-8")
    trace_path = tmp_path / "traces.jsonl"
    monkeypatch.setenv("AUDIT_BACKEND", "jsonl")
    monkeypatch.setenv("LOCAL_FILE_ROOT", str(root))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("TRACE_LOG_PATH", str(trace_path))
    get_settings.cache_clear()

    event = asyncio.run(
        run_guarded_action(
            {
                "user_goal": "read demo file",
                "action_type": "FILE_READ",
                "target_system": "local_file",
                "target": "sample.txt",
                "payload": {"action": "read", "path": "sample.txt"},
            }
        )
    )

    trace = TraceWriter(str(trace_path)).list()[0]
    assert trace.run_id == event.run_id
    assert trace.action_request["action_id"] == event.action_id
    assert trace.latency["total_ms"] >= 0
