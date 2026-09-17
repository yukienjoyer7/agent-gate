"""Run with a clean wheel environment's Python, from outside the checkout.

The only HTTP listener is a test double for the outbound LLM provider. AgentGate
itself runs in-process without an API server or external database.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class LLMStub(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._json({"models": [{"name": "stub"}]})

    def _json(self, value: dict) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert "package-smoke-secret" not in json.dumps(
            request
        ), "A credential leaked into LLM context"
        system = request["messages"][0]["content"]
        if self.path == "/api/chat":
            if "Personally Identifiable Information" in system:
                verdict = {"has_pii": False}
            elif "secret/credential classifier" in system:
                verdict = {"has_secrets": False}
            elif "source-code classifier" in system:
                verdict = {
                    "has_code": False,
                    "has_codename": False,
                    "language": "",
                    "confidence": 1.0,
                }
            elif "payment/phishing classifier" in system:
                verdict = {
                    "has_payment": False,
                    "has_credential_request": False,
                    "has_urgency": False,
                    "confidence": 1.0,
                }
            elif "action-intent classifier" in system:
                verdict = {
                    "is_bulk": False,
                    "estimated_count": 0,
                    "is_destructive": False,
                    "is_external_send": False,
                    "confidence": 1.0,
                }
            else:
                verdict = {"label": "benign", "confidence": 1.0}
            self._json({"message": {"content": json.dumps(verdict)}})
            return
        if "reactive replanner" in system:
            content = {"done": True, "next_steps": [], "explanation": "File read completed"}
        else:
            content = {
                "plan": [
                    {
                        "action_type": "FILE_READ",
                        "target_system": "local_file",
                        "domain": "filesystem",
                        "risk_hint": "file_read",
                        "target": "note.txt",
                        "payload": {"action": "read", "path": "note.txt"},
                    }
                ]
            }
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": json.dumps(content)}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def smoke() -> None:
    for dependency in ("fastapi", "uvicorn", "asyncpg", "psycopg", "redis", "playwright", "stripe"):
        assert (
            importlib.util.find_spec(dependency) is None
        ), f"Not a base-only environment: {dependency}"
    executable = Path(sys.executable).parent / ("agentgate.exe" if os.name == "nt" else "agentgate")
    server = ThreadingHTTPServer(("127.0.0.1", 0), LLMStub)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="agentgate-smoke-") as directory:
            root = Path(directory)
            (root / "note.txt").write_text("hello package-smoke-secret", encoding="utf-8")
            environment = {
                **os.environ,
                "AGENTGATE_CONFIG_DIR": str(root / "config"),
                "AGENTGATE_DATA_DIR": str(root / "data"),
                "LLM_API_KEY": "package-smoke-secret",
                "LLM_URL": f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                "LLM_MODEL": "stub",
                "LLM_TYPE": "openai",
                "GUARDRAIL_BACKEND": "agentgate",
                "OLLAMA_HOST": f"http://127.0.0.1:{server.server_port}",
                "AGENTGATE_LLM_DETECTOR_MODEL": "stub",
                "AGENTGATE_DETECTOR_ARCHITECTURE": "six",
                "DATABASE_URL": "not-a-server-database",
                "GUARDRAIL_BLOCK_HINTS": "[]",
                "GUARDRAIL_NEED_APPROVAL_HINTS": "[]",
            }
            environment.pop("PYTHONPATH", None)

            def command(*arguments: str, expected: int = 0) -> str:
                result = subprocess.run(
                    [str(executable), *arguments],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
                assert result.returncode == expected, result.stderr + result.stdout
                assert "package-smoke-secret" not in result.stdout + result.stderr
                return result.stdout

            assert "run" in command("--help")
            command(
                "init", "--non-interactive", "--workspace", str(root), "--credential-store", "env"
            )
            checks = json.loads(command("doctor", "--json"))
            assert checks["storage"] == "ok"
            assert checks["guardrail"] == "ready (stub)"
            events = [
                json.loads(line)
                for line in command(
                    "run", "Read note.txt", "--json", "--non-interactive"
                ).splitlines()
            ]
            assert events[-1]["type"] == "done" and events[-1]["data"]["status"] == "done"
            run_id = events[0]["run_id"]
            assert json.loads(command("history", "--json"))[0]["run_id"] == run_id
            record = json.loads(command("show", run_id, "--json"))
            assert record["audit"][0]["execution_status"] == "SUCCESS"
            evaluation = json.loads(
                (root / "data" / "guardrail.jsonl").read_text(encoding="utf-8").splitlines()[0]
            )
            assert evaluation["decision"]["decision"] == "ALLOW"
            assert (
                evaluation["audit_id"] == record["audit"][0]["decision_json"]["guardrail_audit_id"]
            )
            assert record["steps"][0]["execution"]["data"]["content_preview"] == "hello [REDACTED]"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    print(
        "Base wheel smoke passed: help, init, doctor, run, history, show, secret masking; no server dependencies"
    )


if __name__ == "__main__":
    smoke()
