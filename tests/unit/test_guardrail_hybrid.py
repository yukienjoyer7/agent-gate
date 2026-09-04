"""Tests for the hybrid guardrail: rules first, optional dedicated LLM judge.

These never touch the network: the LLM judge is either disabled (default) or
replaced with fakes, and the "no API key" path falls back to rules fast.
"""

import asyncio

from app.config.settings import get_settings
from app.core.action_request import build_action_request
from app.core.schemas import Decision, DecisionResponse
from app.domains.guardrail.decision import adecide, decide
from app.domains.guardrail.decision.simple import decide_rule


def _request(**overrides) -> "object":
    proposal = {
        "action_type": "API_CALL",
        "target_system": "gmail",
        "target": "john@example.com",
        "domain": "productivity",
        "risk_hint": "unknown",
        "payload": {"action": "send", "to": "john@example.com"},
        **overrides,
    }
    return build_action_request(proposal)


def test_sync_decide_is_rules_only() -> None:
    assert decide is decide_rule
    assert decide(_request(risk_hint="destructive")).decision == Decision.BLOCK


def test_rule_decision_used_when_llm_disabled() -> None:
    result = asyncio.run(adecide(_request()))
    assert result.decision == Decision.ALLOW


def test_rule_block_never_overridden_by_llm(monkeypatch) -> None:
    monkeypatch.setenv("GUARDRAIL_LLM_ENABLED", "true")
    get_settings.cache_clear()

    async def fake_llm(action):
        return DecisionResponse(
            run_id=action.run_id,
            action_id=action.action_id,
            decision=Decision.ALLOW,
        )

    monkeypatch.setattr("app.domains.guardrail.decision.hybrid.decide_llm", fake_llm)
    try:
        result = asyncio.run(adecide(_request(risk_hint="destructive")))
    finally:
        get_settings.cache_clear()
    assert result.decision == Decision.BLOCK


def test_llm_verdict_replaces_rule_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("GUARDRAIL_LLM_ENABLED", "true")
    get_settings.cache_clear()

    async def fake_llm(action):
        return DecisionResponse(
            run_id=action.run_id,
            action_id=action.action_id,
            decision=Decision.NEED_APPROVAL,
            reasons=["flagged by llm"],
        )

    monkeypatch.setattr("app.domains.guardrail.decision.hybrid.decide_llm", fake_llm)
    try:
        result = asyncio.run(adecide(_request()))
    finally:
        get_settings.cache_clear()
    assert result.decision == Decision.NEED_APPROVAL
    assert "flagged by llm" in result.reasons


def test_llm_failure_falls_back_to_rules(monkeypatch) -> None:
    monkeypatch.setenv("GUARDRAIL_LLM_ENABLED", "true")
    get_settings.cache_clear()

    async def boom(action):
        raise RuntimeError("LLM down")

    monkeypatch.setattr("app.domains.guardrail.decision.hybrid.decide_llm", boom)
    try:
        result = asyncio.run(adecide(_request()))
    finally:
        get_settings.cache_clear()
    assert result.decision == Decision.ALLOW


def test_external_send_still_needs_approval_without_llm() -> None:
    result = asyncio.run(adecide(_request(risk_hint="external_send")))
    assert result.decision == Decision.NEED_APPROVAL
