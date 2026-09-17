import httpx
import pytest

from app.domains.guardrail.services.diagnostics import detector_status


@pytest.mark.asyncio
@pytest.mark.parametrize("models,ready", [([{"name": "stub"}], True), ([], False)])
async def test_doctor_checks_installed_detector_model(monkeypatch, models, ready):
    monkeypatch.setenv("GUARDRAIL_BACKEND", "agentgate")
    monkeypatch.setenv("AGENTGATE_LLM_DETECTOR_MODEL", "stub")

    async def get(self, url):
        return httpx.Response(200, json={"models": models}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    assert (await detector_status())[0] is ready


@pytest.mark.asyncio
async def test_doctor_explains_unavailable_ollama(monkeypatch):
    monkeypatch.setenv("GUARDRAIL_BACKEND", "agentgate")

    async def get(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    ready, message = await detector_status()
    assert not ready and "start Ollama" in message
