"""The production detector suite: full local-LLM classification via Ollama."""

from __future__ import annotations

import os
from typing import Any

from .base import Detector, Finding
from .llm_detectors import (
    LLMActionIntentDetector,
    LLMPaymentPhishingDetector,
    LLMPIIDetector,
    LLMSecretDetector,
    LLMSourceCodeDetector,
)
from .llm_prompt_injection import LLMPromptInjectionDetector
from .llm_unified import LLMUnifiedDetector


def get_default_detectors(
    *,
    model: str | None = None,
    host: str | None = None,
    timeout: float | None = None,
    extra_options: dict[str, Any] | None = None,
) -> list[Detector]:
    """Build a fresh detector suite.

    AGENTGATE_DETECTOR_ARCHITECTURE selects which one:
      six (default) - the six independently-tuned, DA-eval-validated classifiers,
        dispatched concurrently. ~7.4s per action locally (qwen2.5:7b, warm,
        OLLAMA_NUM_PARALLEL=6).
      unified - one combined call covering the same six categories in a single
        Ollama request (docs/ds/06-unified-detector-experiment.md). Correct - it
        passed a full DA eval and all 4 scenarios clean - but measured ~25-30%
        *slower* than "six" on this hardware (~9.5s), not faster: one call has to
        generate a larger combined JSON response sequentially, which costs more
        than concurrent dispatch saves by not re-processing the same input six
        times. Kept available and documented, not the default, for exactly the
        reason this project already learned once before with a similar
        consolidation attempt (git history, `b6017a4`): don't drop a working,
        validated architecture for an unvalidated "simpler" one without measuring
        first - and here, once measured, it wasn't actually better.

    Legacy regex/hybrid implementations remain in the repository for historical
    benchmarks and deterministic sanitization patterns, but are not reachable through
    normal runtime configuration.
    """
    options = {
        "model": model,
        "host": host,
        "timeout": timeout,
        "extra_options": extra_options,
    }
    architecture = os.environ.get("AGENTGATE_DETECTOR_ARCHITECTURE", "six").lower()
    if architecture == "unified":
        return [LLMUnifiedDetector(**options)]
    if architecture != "six":
        raise ValueError(
            f"Unknown AGENTGATE_DETECTOR_ARCHITECTURE {architecture!r}; use 'six' or 'unified'"
        )
    return [
        LLMPIIDetector(**options),
        LLMSecretDetector(**options),
        LLMSourceCodeDetector(**options),
        LLMPaymentPhishingDetector(**options),
        LLMPromptInjectionDetector(**options),
        LLMActionIntentDetector(**options),
    ]


__all__ = [
    "Detector",
    "Finding",
    "LLMPIIDetector",
    "LLMSecretDetector",
    "LLMSourceCodeDetector",
    "LLMPaymentPhishingDetector",
    "LLMPromptInjectionDetector",
    "LLMActionIntentDetector",
    "LLMUnifiedDetector",
    "get_default_detectors",
]
