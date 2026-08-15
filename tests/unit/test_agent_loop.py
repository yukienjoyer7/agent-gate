"""Tests for the reactive agent loop (app.domains.agent.services.agent_loop).

Everything is faked — no real LLM, browser or audit store — so the tests are
fast and hermetic. The rule-based guardrail runs for real (it is
deterministic and instant).
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.run_schema import RunStatus, StepStatus
from app.core.schemas import AuditEvent, ExecutionStatus
from app.domains.agent.services import agent_loop
from app.domains.agent.services.run_registry import run_registry


class _FakeAuditRepo:
    def __init__(self) -> None:
        self.writes: list = []

    async def write(self, request, decision, execution, latency=None):
        self.writes.append((request, decision, execution))
        return SimpleNamespace(model_dump=lambda mode="json": {"run_id": request.run_id})


def _event(
    run_id: str, action_id: str, status=ExecutionStatus.SUCCESS, data=None, error=None
) -> AuditEvent:
    return AuditEvent(
        run_id=run_id,
        action_id=action_id,
        request_json={},
        decision_json={},
        execution_json={
            "result_summary": "ok" if status == ExecutionStatus.SUCCESS else "boom",
            "data": data or {},
            "error": error,
        },
        execution_status=status,
    )


def _open_step(url: str = "https://example.test") -> dict:
    return {
        "action_type": "BROWSER_OPEN",
        "target_system": "browser",
        "target": url,
        "domain": "browser",
        "risk_hint": "unknown",
        "payload": {"url": url},
    }


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _no_llm_or_io(monkeypatch):
    """Defaults: replanner says "done", audit repo is a fake."""

    async def fake_replan(prompt, context):
        return []

    monkeypatch.setattr(agent_loop, "parse_next_steps", fake_replan)
    monkeypatch.setattr(agent_loop, "get_audit_repository", lambda: _FakeAuditRepo())


@pytest.mark.asyncio
async def test_happy_browser_path(monkeypatch):
    browser_calls: list[dict] = []

    async def fake_plan(prompt):
        return {
            "plan": [_open_step()],
            "llm_provider": "dummy",
            "raw_prompt": prompt,
            "human_readable": "",
        }

    async def fake_browser(**kwargs):
        browser_calls.append(kwargs)
        return _event(kwargs["run_id"], kwargs["action_id"], data={"final_url": kwargs["url"]})

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent", fake_browser)

    run = run_registry.create("open example")
    await asyncio.wait_for(agent_loop.run_agent_loop(run), timeout=5)

    assert run.status == RunStatus.DONE
    assert run.steps[0].status == StepStatus.DONE
    assert browser_calls[0]["run_id"] == run.run_id
    assert browser_calls[0]["skip_guardrail"] is True


@pytest.mark.asyncio
async def test_approval_then_execute(monkeypatch):
    guarded_calls: list = []

    async def fake_plan(prompt):
        return {
            "plan": [
                {
                    "action_type": "API_CALL",
                    "target_system": "gmail",
                    "target": "john@example.com",
                    "domain": "productivity",
                    "risk_hint": "external_send",
                    "payload": {"action": "send", "to": "john@example.com"},
                }
            ],
            "llm_provider": "dummy",
            "raw_prompt": prompt,
            "human_readable": "",
        }

    async def fake_guarded(proposal, audit=None, traces=None, decision=None):
        guarded_calls.append((proposal, decision))
        return _event(proposal["run_id"], proposal["action_id"])

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "run_guarded_action", fake_guarded)

    run = run_registry.create("send email")
    task = asyncio.create_task(agent_loop.run_agent_loop(run))
    await _wait_for(lambda: run.status == RunStatus.WAITING_APPROVAL)
    assert run.steps[0].status == StepStatus.WAITING_APPROVAL

    run_registry.respond(run, 0, "approve")
    await asyncio.wait_for(task, timeout=5)

    assert run.status == RunStatus.DONE
    assert run.steps[0].status == StepStatus.DONE
    proposal, decision = guarded_calls[0]
    assert decision.decision.value == "ALLOW"
    assert "approved by user" in decision.reasons
    assert proposal["run_id"] == run.run_id


@pytest.mark.asyncio
async def test_decline_stops_run(monkeypatch):
    guarded_calls: list = []

    async def fake_plan(prompt):
        return {
            "plan": [
                {
                    "action_type": "API_CALL",
                    "target_system": "gmail",
                    "target": "john@example.com",
                    "domain": "productivity",
                    "risk_hint": "external_send",
                    "payload": {"action": "send", "to": "john@example.com"},
                }
            ],
            "llm_provider": "dummy",
            "raw_prompt": prompt,
            "human_readable": "",
        }

    async def fake_guarded(proposal, audit=None, traces=None, decision=None):
        guarded_calls.append(proposal)
        return _event(proposal["run_id"], proposal["action_id"])

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "run_guarded_action", fake_guarded)

    run = run_registry.create("send email")
    task = asyncio.create_task(agent_loop.run_agent_loop(run))
    await _wait_for(lambda: run.status == RunStatus.WAITING_APPROVAL)

    run_registry.respond(run, 0, "decline")
    await asyncio.wait_for(task, timeout=5)

    assert run.status == RunStatus.DECLINED
    assert run.steps[0].status == StepStatus.DECLINED
    assert guarded_calls == []  # never executed


@pytest.mark.asyncio
async def test_blocked_step_stops_run(monkeypatch):
    browser_calls: list = []

    async def fake_plan(prompt):
        return {
            "plan": [
                {
                    "action_type": "API_CALL",
                    "target_system": "github",
                    "target": "octo/demo",
                    "domain": "code_protection",
                    "risk_hint": "destructive",
                    "payload": {"action": "delete_repo", "owner": "octo", "repo": "demo"},
                }
            ],
            "llm_provider": "dummy",
            "raw_prompt": prompt,
            "human_readable": "",
        }

    async def fake_guarded(proposal, audit=None, traces=None, decision=None):
        browser_calls.append(proposal)
        return _event(proposal["run_id"], proposal["action_id"])

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "run_guarded_action", fake_guarded)

    run = run_registry.create("delete repo")
    await asyncio.wait_for(agent_loop.run_agent_loop(run), timeout=5)

    assert run.status == RunStatus.BLOCKED
    assert run.steps[0].status == StepStatus.BLOCKED
    assert browser_calls == []  # never executed
    # audit was written for the blocked action
    assert run.steps[0].audit_event is not None


@pytest.mark.asyncio
async def test_sanitize_input_then_execute(monkeypatch):
    """A {{password}} placeholder pauses the step; the user's text is used."""
    browser_calls: list[dict] = []

    async def fake_plan(prompt):
        return {
            "plan": [
                {
                    "action_type": "BROWSER_TYPE",
                    "target_system": "browser",
                    "target": "https://example.test",
                    "domain": "browser",
                    "risk_hint": "unknown",
                    "payload": {
                        "url": "https://example.test",
                        "label": "Password",
                        "role": "textbox",
                        "value": "{{password}}",
                    },
                }
            ],
            "llm_provider": "dummy",
            "raw_prompt": prompt,
            "human_readable": "",
        }

    async def fake_browser(**kwargs):
        browser_calls.append(kwargs)
        return _event(kwargs["run_id"], kwargs["action_id"], data={"final_url": kwargs["url"]})

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent", fake_browser)

    run = run_registry.create("login")
    task = asyncio.create_task(agent_loop.run_agent_loop(run))
    await _wait_for(lambda: run.status == RunStatus.WAITING_INPUT)
    assert run.steps[0].status == StepStatus.WAITING_INPUT

    run_registry.respond(run, 0, "input", fields={"value": "s3cret!"})
    await asyncio.wait_for(task, timeout=5)

    assert run.status == RunStatus.DONE
    assert run.steps[0].status == StepStatus.DONE
    assert "value" in run.steps[0].answered
    assert run.steps[0].data["payload"]["value"] == "s3cret!"
    assert browser_calls[0]["actions"][0]["value"] == "s3cret!"


@pytest.mark.asyncio
async def test_replan_after_failure(monkeypatch):
    """A failed browser batch triggers the replanner, which appends a step."""
    browser_calls: list[dict] = []
    replan_calls: list[dict] = []

    async def fake_plan(prompt):
        return {
            "plan": [_open_step()],
            "llm_provider": "dummy",
            "raw_prompt": prompt,
            "human_readable": "",
        }

    async def fake_browser(**kwargs):
        browser_calls.append(kwargs)
        if len(browser_calls) == 1:
            return _event(
                kwargs["run_id"],
                kwargs["action_id"],
                status=ExecutionStatus.FAILED,
                error={"code": "BROWSER_TIMEOUT", "message": "login required"},
            )
        return _event(kwargs["run_id"], kwargs["action_id"], data={"final_url": kwargs["url"]})

    async def fake_replan(prompt, context):
        replan_calls.append(context)
        if len(replan_calls) == 1:
            return [
                {
                    "action_type": "BROWSER_CLICK",
                    "target_system": "browser",
                    "target": "https://example.test",
                    "domain": "browser",
                    "risk_hint": "unknown",
                    "payload": {"label": "Continue", "role": "button"},
                }
            ]
        return []

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "parse_next_steps", fake_replan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent", fake_browser)

    run = run_registry.create("open example")
    await asyncio.wait_for(agent_loop.run_agent_loop(run), timeout=5)

    assert run.status == RunStatus.DONE
    assert len(run.steps) == 2
    assert run.steps[0].status == StepStatus.FAILED
    assert run.steps[1].status == StepStatus.DONE
    # one replan after the failure + one completion check
    assert run.replan_count == 2
    # the replanner saw the failure observation in its context
    assert "login required" in replan_calls[0]["latest_observation"]
    assert replan_calls[0]["executed_steps"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_max_steps_cap(monkeypatch):
    """The replanner's steps are capped by AGENT_MAX_STEPS."""

    async def fake_plan(prompt):
        return {
            "plan": [_open_step()],
            "llm_provider": "dummy",
            "raw_prompt": prompt,
            "human_readable": "",
        }

    async def fake_browser(**kwargs):
        return _event(
            kwargs["run_id"],
            kwargs["action_id"],
            status=ExecutionStatus.FAILED,
            error={"code": "X", "message": "always fails"},
        )

    async def fake_replan(prompt, context):
        return [_open_step()]  # keep appending forever

    # Patch the name bound inside agent_loop (module-level import) so the loop
    # actually sees the tightened caps.
    monkeypatch.setattr(
        agent_loop,
        "get_settings",
        lambda: SimpleNamespace(
            AGENT_MAX_STEPS=3,
            AGENT_MAX_REPLAN=4,
            AGENT_WAIT_RESPONSE_TIMEOUT_SEC=5.0,
            BROWSER_SETTLE_MS=0,
            GUARDRAIL_LLM_ENABLED=False,
        ),
    )
    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "parse_next_steps", fake_replan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent", fake_browser)

    run = run_registry.create("loop")
    await asyncio.wait_for(agent_loop.run_agent_loop(run), timeout=5)

    assert len(run.steps) <= 3
