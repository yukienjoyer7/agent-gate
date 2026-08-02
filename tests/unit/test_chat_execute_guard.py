"""Tests for app.api.v1.chat execute routing guards."""

import asyncio
from types import SimpleNamespace

from app.api.v1.chat import _execute_browser, _plan_to_browser_actions


class _FakeAuditRepo:
    """Hermetic stand-in: never touches the real (Postgres) audit store."""

    def __init__(self) -> None:
        self.writes: list = []

    async def write(self, request, decision, execution, latency=None):
        event = {
            "execution_status": execution.status,
            "error_type": (execution.error or {}).get("code"),
            "execution_json": execution.model_dump(mode="json"),
        }
        self.writes.append(event)
        return SimpleNamespace(model_dump=lambda mode="json": event)


def test_execute_browser_rejects_plan_without_url(monkeypatch) -> None:
    """A browser plan with no URL must return an AuditEvent FAILED with
    INVALID_PLAN — not page.goto(\"\") and not a bare 400 detail."""
    fake = _FakeAuditRepo()
    monkeypatch.setattr(
        "app.domains.audit.repositories.get_audit_repository", lambda: fake
    )

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
            "payload": {"label": "Search", "element_id": "search_query", "role": "combobox"},
        },
    ]
    actions = _plan_to_browser_actions(steps)

    assert [action["type"] for action in actions] == ["fill", "submit"]
    assert actions[1]["element_id"] == "search_query"
    assert actions[0]["value"] == "jerome"
