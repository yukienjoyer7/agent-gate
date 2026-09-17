"""Read-only detector readiness check for the local CLI."""

import os

import httpx

from app.domains.guardrail._vendor.agentgate.detectors.llm_client import (
    LLMUnavailable,
    resolve_host,
    resolve_model,
)


async def detector_status() -> tuple[bool, str]:
    backend = os.environ.get("GUARDRAIL_BACKEND", "agentgate")
    if backend == "legacy":
        return True, "legacy backend explicitly selected"
    if backend != "agentgate":
        return False, "GUARDRAIL_BACKEND must be agentgate or legacy"
    try:
        host, model = resolve_host(), resolve_model()
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(host + "/api/tags")
            response.raise_for_status()
            models = response.json().get("models", [])
        if any(item.get("name") in {model, model + ":latest"} for item in models):
            return True, f"ready ({model})"
        return False, f"model missing; run 'ollama pull {model}'"
    except (httpx.HTTPError, LLMUnavailable, ValueError, TypeError, AttributeError):
        return False, "unavailable; start Ollama and check OLLAMA_HOST"
