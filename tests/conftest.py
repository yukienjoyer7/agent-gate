"""Keep regression tests independent of developer credentials and services.

Existing tests exercise the legacy policy contract. Embedded-engine tests
explicitly select agentgate and fake only the Ollama transport.
"""

import pytest

from app.config.settings import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("GUARDRAIL_BACKEND", "legacy")
    monkeypatch.setenv("GUARDRAIL_LLM_ENABLED", "false")
    monkeypatch.setenv("AGENTGATE_REDIS_QUEUE_ENABLED", "false")
    monkeypatch.setenv("LLM_TYPE", "openai")
    monkeypatch.setenv("ATOMIC_BROWSER_AUDIT", "false")
    monkeypatch.setenv("AUDIT_BACKEND", "jsonl")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("GUARDRAIL_AUDIT_PATH", str(tmp_path / "guardrail.jsonl"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
