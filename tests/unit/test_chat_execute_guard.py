"""Tests for app.api.v1.chat execute routing guards."""

import asyncio
from types import SimpleNamespace

from app.api.v1.chat import _execute_browser, _plan_to_browser_actions
from app.core.action_request import build_action_request
from app.core.schemas import Decision, ExecutionStatus
from app.domains.guardrail.decision import decide
from app.executors.router import decision_to_execution_status


class _FakeAuditRepo:
    """Hermetic stand-in: never touches the real (Postgres) audit store."""

    def __init__(self) -> None:
        self.writes: list = []

    async def write(self, request, decision, execution, latency=None):
        event = {
            "execution_status": execution.status,
            "error_type": (execution.error or {}).get("code"),
            "decision_json": decision.model_dump(mode="json"),
            "execution_json": execution.model_dump(mode="json"),
        }
        self.writes.append(event)
        return SimpleNamespace(model_dump=lambda mode="json": event)


def test_execute_browser_rejects_plan_without_url(monkeypatch) -> None:
    """A browser plan with no URL must return an AuditEvent FAILED with
    INVALID_PLAN — not page.goto(\"\") and not a bare 400 detail."""
    fake = _FakeAuditRepo()
    monkeypatch.setattr("app.domains.audit.repositories.get_audit_repository", lambda: fake)

    steps = [
        {
            "source": "chat",
            "domain": "browser",
            "action_type": "BROWSER_CLICK",
            "target_system": "browser",
            "target": "",
            "risk_hint": "unknown",
            "payload": {"label": "Search"},
        }
    ]
    event = asyncio.run(_execute_browser(steps, "click search"))

    assert event["execution_status"] == "FAILED"
    assert event["error_type"] == "INVALID_PLAN"
    assert "no browser URL" in event["execution_json"]["result_summary"]


def test_plan_step_with_secret_payload_sanitizes_before_execution() -> None:
    """A browser step whose payload embeds a secret must be redacted by the
    guardrail (SANITIZE → SANITIZED) — the raw secret must never reach
    Playwright."""
    request = build_action_request(
        {
            "source": "chat",
            "domain": "browser",
            "action_type": "BROWSER_TYPE",
            "target_system": "browser",
            "target": "https://example.com",
            "risk_hint": "unknown",
            "payload": {"label": "API key", "value": "sk-1234567890abcdefgh"},
        }
    )
    response = decide(request)

    assert response.decision == Decision.SANITIZE
    assert response.sanitized_payload == {"label": "API key", "value": "[REDACTED]"}
    assert response.sensitive_entities == ["api_key"]
    assert decision_to_execution_status(response.decision) == ExecutionStatus.SANITIZED


def test_plan_step_needing_clarification_waits_for_user() -> None:
    """A browser step with an ambiguous target must yield ASK_USER
    (WAITING_USER) — it must never reach Playwright."""
    request = build_action_request(
        {
            "source": "chat",
            "domain": "browser",
            "action_type": "BROWSER_CLICK",
            "target_system": "browser",
            "target": "https://example.com",
            "risk_hint": "ambiguous_target",
            "payload": {"label": "Continue"},
        }
    )
    response = decide(request)

    assert response.decision == Decision.ASK_USER
    assert any("clarification" in reason for reason in response.reasons)
    assert decision_to_execution_status(response.decision) == ExecutionStatus.WAITING_USER


def test_plan_to_browser_actions_maps_submit() -> None:
    """BROWSER_SUBMIT must map to the submit action (Enter-press), not click."""
    steps = [
        {
            "action_type": "BROWSER_OPEN",
            "target_system": "browser",
            "target": "https://www.youtube.com",
            "payload": {"url": "https://www.youtube.com"},
        },
        {
            "action_type": "BROWSER_TYPE",
            "target_system": "browser",
            "target": "https://www.youtube.com",
            "payload": {
                "label": "Search",
                "element_id": "search_query",
                "role": "combobox",
                "value": "jerome",
            },
        },
        {
            "action_type": "BROWSER_SUBMIT",
            "target_system": "browser",
            "target": "https://www.youtube.com",
            "payload": {
                "label": "Search",
                "element_id": "search_query",
                "role": "combobox",
                "delay_ms": 2000,
            },
        },
    ]
    actions = _plan_to_browser_actions(steps)

    assert [action["type"] for action in actions] == ["fill", "submit"]
    assert actions[1]["element_id"] == "search_query"
    assert actions[1]["delay_ms"] == 2000
    assert actions[0]["value"] == "jerome"
