import asyncio
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from app.cli.main import main
from app.core.schemas import ExecutionResult, ExecutionStatus
from app.runtime.config import LocalConfig, LocalPaths, load_config, save_config
from app.runtime.safety import Sanitizer
from app.storage.local.database import LocalDatabase


@pytest.fixture
def local_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTGATE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGENTGATE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_API_KEY", "unit-test-credential-do-not-log")
    paths = LocalPaths.resolve()
    config = LocalConfig(workspace=str(tmp_path), credential_store="env")
    save_config(paths.config, config)
    return paths, config


def patch_plan(monkeypatch, steps):
    from app.domains.agent.services import agent_loop

    async def plan(prompt):
        return {"plan": steps}

    async def replan(prompt, context):
        return []

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", plan)
    monkeypatch.setattr(agent_loop, "parse_next_steps", replan)


def calendar_step(**fields):
    return {
        "action_type": "API_CALL",
        "target_system": "calendar",
        "target": "primary",
        "domain": "productivity",
        "risk_hint": "unknown",
        "payload": {"action": "create_event", **fields},
    }


def test_help_and_local_imports_need_no_server_or_browser():
    script = """
import importlib.abc
import sys
class RejectServer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi', 'uvicorn', 'asyncpg', 'psycopg', 'redis', 'playwright', 'stripe'} or fullname == 'app.database.session':
            raise AssertionError('Unexpected import: ' + fullname)
sys.meta_path.insert(0, RejectServer())
from app.runtime.service import AgentService
from app.cli.main import main
main(['--help'])
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "run" in result.stdout


def test_noninteractive_init_roundtrip_and_preserves_existing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTGATE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGENTGATE_DATA_DIR", str(tmp_path / "data"))
    arguments = [
        "init",
        "--non-interactive",
        "--workspace",
        str(tmp_path),
        "--credential-store",
        "env",
    ]
    assert main(arguments) == 0
    paths = LocalPaths.resolve()
    original = paths.config.read_bytes()
    assert load_config(paths.config).workspace == str(tmp_path)
    assert main(arguments) == 0
    assert paths.config.read_bytes() == original
    if os.name != "nt":
        assert paths.config.stat().st_mode & 0o777 == 0o600
        assert paths.database.stat().st_mode & 0o777 == 0o600
    assert "preserved" in capsys.readouterr().out


def test_project_configuration_cannot_weaken_policy(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("GUARDRAIL_BLOCK_HINTS = []\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_local_file_run_persists_safe_history(local_profile, monkeypatch, tmp_path, capsys):
    paths, _ = local_profile
    (tmp_path / "note.txt").write_text("hello unit-test-credential-do-not-log")
    patch_plan(
        monkeypatch,
        [
            {
                "action_type": "FILE_READ",
                "target_system": "local_file",
                "domain": "filesystem",
                "target": "note.txt",
                "risk_hint": "file_read",
                "payload": {"action": "read", "path": "note.txt"},
            }
        ],
    )
    assert main(["run", "Read note.txt", "--json", "--non-interactive"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "done"
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    with LocalDatabase(paths.database) as database:
        row = database.db.execute("SELECT * FROM runs").fetchone()
        assert row["status"] == "done"
        run_id = row["run_id"]
        assert database.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
        assert database.db.execute("SELECT state FROM intents").fetchone()[0] == "completed"
    assert main(["show", run_id, "--json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["steps"][0]["status"] == "done"
    assert "unit-test-credential-do-not-log" not in json.dumps(record)
    assert main(["history", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["run_id"] == run_id


def test_noninteractive_write_fails_closed(local_profile, monkeypatch, capsys):
    paths, _ = local_profile
    patch_plan(
        monkeypatch,
        [
            calendar_step(
                summary="Demo", start="2026-09-14T10:00:00+07:00", end="2026-09-14T10:30:00+07:00"
            )
        ],
    )
    assert main(["run", "Create Demo", "--json", "--non-interactive"]) == 3
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[-1]["type"] == "interaction_required"
    with LocalDatabase(paths.database) as database:
        assert (
            database.db.execute("SELECT status FROM runs").fetchone()[0] == "interaction_required"
        )
        assert database.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0


def scripted_terminal(monkeypatch, answers):
    from app.cli import console

    original = console.Console

    class ScriptedConsole(original):
        @property
        def interactive(self):
            return True

        async def read(self, label, **kwargs):
            return answers.pop(0)

    monkeypatch.setattr(console, "Console", ScriptedConsole)


def test_clarification_then_exact_action_approval(local_profile, monkeypatch, capsys):
    paths, _ = local_profile
    patch_plan(monkeypatch, [calendar_step(summary="Demo")])
    scripted_terminal(monkeypatch, ["2026-09-14T10:00:00+07:00", "2026-09-14T10:30:00+07:00", "y"])
    calls = []
    from app.runtime.local import LazyConnector

    async def execute(self, action, payload):
        calls.append(payload)
        # The intent must have committed before the external operation starts.
        with LocalDatabase(paths.database) as database:
            assert database.db.execute("SELECT state FROM intents").fetchone()[0] == "started"
        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="calendar",
            status=ExecutionStatus.SUCCESS,
            result_summary="Created Demo",
        )

    monkeypatch.setattr(LazyConnector, "execute", execute)
    assert main(["run", "Create Demo"]) == 0
    assert len(calls) == 1
    assert calls[0]["start"] == "2026-09-14T10:00:00+07:00"
    with LocalDatabase(paths.database) as database:
        events = [
            json.loads(row[0])["type"]
            for row in database.db.execute("SELECT event_json FROM events ORDER BY sequence")
        ]
        assert (
            events.index("awaiting_input")
            < events.index("awaiting_approval")
            < events.index("executing")
        )
    capsys.readouterr()


def test_decline_never_executes(local_profile, monkeypatch, capsys):
    paths, _ = local_profile
    patch_plan(
        monkeypatch,
        [
            calendar_step(
                summary="Demo", start="2026-09-14T10:00:00+07:00", end="2026-09-14T10:30:00+07:00"
            )
        ],
    )
    scripted_terminal(monkeypatch, ["n"])
    assert main(["run", "Create Demo"]) == 1
    with LocalDatabase(paths.database) as database:
        assert database.db.execute("SELECT status FROM runs").fetchone()[0] == "declined"
        assert database.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0
        assert database.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
    capsys.readouterr()


def test_sanitizer_masks_nested_secrets_and_terminal_controls():
    sanitizer = Sanitizer()
    sanitizer.remember("custom-unstructured-secret")
    cleaned = sanitizer.clean(
        {
            "prompt": "custom-unstructured-secret",
            "payload": {"password": "hunter2"},
            "action_type": "BROWSER_TYPE",
            "value": "user input",
            "message": "person@example.com\x1b[31m",
        }
    )
    encoded = json.dumps(cleaned)
    assert "hunter2" not in encoded
    assert "custom-unstructured-secret" not in encoded
    assert "user input" not in encoded
    assert "person@example.com" not in encoded
    assert "\\u001b" not in encoded
    assert "nested typed value" not in json.dumps(
        sanitizer.clean({"action_type": "BROWSER_TYPE", "payload": {"value": "nested typed value"}})
    )


def test_local_run_ignores_server_environment_and_cannot_weaken_policy(local_profile, monkeypatch):
    paths, config = local_profile
    monkeypatch.setenv("DATABASE_URL", "not-a-database")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "not-an-integer")
    monkeypatch.setenv("GUARDRAIL_BLOCK_HINTS", "[]")
    monkeypatch.setenv("GUARDRAIL_NEED_APPROVAL_HINTS", "[]")
    monkeypatch.setenv("ALLOWED_FILESYSTEM_PATHS", '["/"]')
    from app.runtime.local import LocalRuntime

    with LocalRuntime(config, paths) as runtime:
        assert "destructive" in runtime.settings.GUARDRAIL_BLOCK_HINTS
        assert "payment" in runtime.settings.GUARDRAIL_NEED_APPROVAL_HINTS
        assert runtime.settings.ALLOWED_FILESYSTEM_PATHS == [config.workspace]


@pytest.mark.asyncio
async def test_pending_terminal_input_is_cancelled_when_run_finishes():
    from types import SimpleNamespace
    from app.cli.commands import _read_for_run

    closed = asyncio.Event()

    class WaitingConsole:
        async def read(self, *args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    task = asyncio.create_task(asyncio.sleep(0.01))
    assert await _read_for_run(WaitingConsole(), SimpleNamespace(task=task), "Approve?") is None
    assert closed.is_set()


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal test")
@pytest.mark.asyncio
async def test_secret_input_restores_terminal_echo_on_completion_and_cancel():
    import io
    import pty
    import termios
    from app.cli.console import Console

    master, slave = pty.openpty()
    try:
        with os.fdopen(os.dup(slave), "r") as stream:
            console = Console(Sanitizer(), stdin=stream, stderr=io.StringIO())
            original = termios.tcgetattr(slave)
            reading = asyncio.create_task(console.read("Secret", hidden=True))
            await asyncio.sleep(0.01)
            assert not termios.tcgetattr(slave)[3] & termios.ECHO
            os.write(master, b"private-value\n")
            assert await reading == "private-value"
            assert termios.tcgetattr(slave) == original
            reading = asyncio.create_task(console.read("Secret", hidden=True))
            await asyncio.sleep(0.01)
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
            assert termios.tcgetattr(slave) == original
    finally:
        os.close(master)
        os.close(slave)


def test_local_schema_is_versioned_and_audit_is_append_only(tmp_path):
    path = tmp_path / "data" / "state.sqlite3"
    with LocalDatabase(path) as database:
        assert database.db.execute("PRAGMA user_version").fetchone()[0] == 1
        with database.db as db:
            db.execute("INSERT INTO audit VALUES('action','run','{}')")
        with pytest.raises(sqlite3.IntegrityError):
            database.db.execute("DELETE FROM audit")
    with LocalDatabase(path) as database:
        assert database.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1


def test_crash_recovery_and_profile_lock(local_profile):
    paths, config = local_profile
    from app.runtime.local import LocalRuntime

    with LocalRuntime(config, paths) as runtime:
        with runtime.database.db as db:
            db.execute(
                "INSERT INTO runs(run_id,prompt,status,created_at) VALUES('abandoned','demo','running','now')"
            )
        with pytest.raises(ValueError, match="Another AgentGate"):
            with LocalRuntime(config, paths):
                pass
    with LocalRuntime(config, paths) as runtime:
        assert runtime.runs.get("abandoned")["status"] == "interrupted"


@pytest.mark.asyncio
async def test_cancellation_closes_run_and_runtime(local_profile, monkeypatch):
    paths, config = local_profile
    from app.domains.agent.services import run_service
    from app.runtime.context import current_runtime
    from app.runtime.local import LocalRuntime
    from app.runtime.service import AgentService

    entered, closed = asyncio.Event(), asyncio.Event()

    async def loop(run):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(run_service, "run_agent_loop", loop)
    with LocalRuntime(config, paths) as runtime:
        service = AgentService(runtime)
        run = service.submit("Wait")
        await entered.wait()
        await service.cancel()
        assert closed.is_set()
        assert run.status.value == "cancelled"
        runtime.runs.finish(run)
    assert current_runtime() is None
    assert runtime.database.connection is None
