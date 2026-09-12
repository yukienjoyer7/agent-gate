"""Tests for the reactive agent loop (app.domains.agent.services.agent_loop).

Everything is faked — no real LLM, browser or audit store — so the tests are
fast and hermetic. The rule-based guardrail runs for real (it is
deterministic and instant).
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.run_schema import RunStatus, StepStatus
from app.core.schemas import AuditEvent, Decision, DecisionResponse, ExecutionStatus
from app.domains.agent.services import agent_loop
from app.domains.agent.services.run_registry import StepState, run_registry
from app.domains.connector.telegram.contacts import TelegramContactIdentity
from app.domains.connector.telegram.recipient_resolver import (
    RecipientResolution,
    RecipientResolutionStatus,
)


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


def _telegram_send_step(reference: str = "Rafi Ahmad") -> dict:
    return {
        "action_type": "API_CALL",
        "target_system": "telegram",
        "target": reference,
        "domain": "productivity",
        "risk_hint": "external_send",
        "payload": {"action": "send_message", "recipient": reference, "text": "halo"},
    }


class _FakeTelegramResolver:
    def __init__(self, resolution: RecipientResolution) -> None:
        self.resolution = resolution
        self.references: list[object] = []

    async def resolve(self, reference: object) -> RecipientResolution:
        self.references.append(reference)
        return self.resolution


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
    monkeypatch.setenv("ATOMIC_BROWSER_AUDIT", "false")


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
async def test_browser_open_replans_before_done_for_incomplete_objective(monkeypatch):
    """Opening a page is only an observation when the prompt requests a click."""
    browser_calls: list[dict] = []
    replan_contexts: list[dict] = []

    async def fake_plan(prompt):
        return {"plan": [_open_step()], "llm_provider": "dummy", "raw_prompt": prompt}

    async def fake_replan(prompt, context):
        replan_contexts.append(context)
        if len(replan_contexts) == 1:
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

    async def fake_browser(**kwargs):
        browser_calls.append(kwargs)
        return _event(
            kwargs["run_id"],
            kwargs["action_id"],
            data={
                "final_url": kwargs["url"],
                "final_snapshot": [
                    {
                        "element_id": "continue",
                        "role": "button",
                        "label": "Continue",
                    }
                ],
            },
        )

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "parse_next_steps", fake_replan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent", fake_browser)

    run = run_registry.create("Open the page, then click Continue and report the result")
    await asyncio.wait_for(agent_loop.run_agent_loop(run), timeout=5)

    assert run.status == RunStatus.DONE
    assert len(browser_calls) == 2
    assert len(replan_contexts) == 2
    assert replan_contexts[0]["completion_check"] == {
        "navigation_only": True,
        "follow_up_required": True,
    }
    assert replan_contexts[0]["latest_observation"]
    assert [step.data["action_type"] for step in run.steps] == [
        "BROWSER_OPEN",
        "BROWSER_CLICK",
    ]


@pytest.mark.asyncio
async def test_browser_open_cannot_be_declared_done_when_replanner_returns_no_action(
    monkeypatch,
):
    async def fake_plan(prompt):
        return {"plan": [_open_step()], "llm_provider": "dummy", "raw_prompt": prompt}

    async def fake_browser(**kwargs):
        return _event(kwargs["run_id"], kwargs["action_id"], data={"final_url": kwargs["url"]})

    async def fake_replan(prompt, context):
        return []

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "parse_next_steps", fake_replan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent", fake_browser)

    run = run_registry.create("Open the page and fill the email form")
    await asyncio.wait_for(agent_loop.run_agent_loop(run), timeout=5)

    assert run.status == RunStatus.FAILED
    assert any(event["type"] == "error" for event in run.events._queue)


@pytest.mark.asyncio
async def test_browser_session_cleanup_runs_on_cancellation(monkeypatch):
    started = asyncio.Event()
    cleanup_calls: list[str] = []

    async def fake_plan(prompt):
        return {"plan": [_open_step()], "llm_provider": "dummy", "raw_prompt": prompt}

    async def fake_browser(**kwargs):
        started.set()
        await asyncio.Event().wait()

    async def fake_cleanup(run_id):
        cleanup_calls.append(run_id)

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent", fake_browser)
    monkeypatch.setattr(agent_loop, "close_browser_session", fake_cleanup)

    run = run_registry.create("open example")
    task = asyncio.create_task(agent_loop.run_agent_loop(run))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert run.status == RunStatus.CANCELLED
    assert cleanup_calls == [run.run_id]


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
async def test_browser_payment_click_requires_approval(monkeypatch):
    step = StepState(
        index=0,
        action_id="act_pay",
        data={
            "action_type": "BROWSER_CLICK",
            "target_system": "browser",
            "target": "https://checkout.example.test",
            "domain": "browser",
            "risk_hint": "unknown",
            "payload": {"label": "Pay now", "role": "button"},
        },
    )
    run = run_registry.create("click the Pay now button")
    run.steps.append(step)

    task = asyncio.create_task(agent_loop._guardrail_step(run, step))
    await _wait_for(lambda: run.status == RunStatus.WAITING_APPROVAL)

    assert step.decision["decision"] == Decision.NEED_APPROVAL
    assert step.decision["risk_level"] == "CRITICAL"
    run_registry.respond(run, 0, "approve")
    decision = await asyncio.wait_for(task, timeout=5)

    assert decision is not None
    assert decision.decision == Decision.ALLOW
    assert decision.initial_decision == Decision.NEED_APPROVAL


@pytest.mark.asyncio
async def test_connector_step_result_includes_structured_execution(monkeypatch):
    checkout_url = "https://checkout.stripe.com/c/pay/cs_test_structured"

    async def fake_guarded(proposal, audit=None, traces=None, decision=None):
        return _event(
            proposal["run_id"],
            proposal["action_id"],
            data={"id": "cs_test_structured", "url": checkout_url},
        )

    monkeypatch.setattr(agent_loop, "run_guarded_action", fake_guarded)

    run = run_registry.create("create checkout")
    step = StepState(
        index=0,
        data={
            "action_type": "API_CALL",
            "target_system": "stripe",
            "target": "workshop_ticket",
            "payload": {
                "action": "create_checkout_session",
                "catalog_key": "workshop_ticket",
                "quantity": 1,
            },
        },
        action_id="act_checkout_structured",
    )
    decision = DecisionResponse(
        run_id=run.run_id,
        action_id=step.action_id,
        decision=Decision.ALLOW,
    )

    await agent_loop._execute_connector_steps(run, [(step, decision)])

    events = list(run.events._queue)
    step_result = next(event for event in events if event["type"] == "step_result")
    assert step_result["data"]["result"]["data"]["url"] == checkout_url


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
async def test_resolved_telegram_recipient_waits_for_approval_then_uses_numeric_chat_id(
    monkeypatch,
):
    guarded_calls: list[tuple[dict, object]] = []
    contact = TelegramContactIdentity(
        chat_id=123456789,
        chat_type="private",
        username="rafiahmad",
        first_name="Rafi",
        last_name="Ahmad",
        display_name="Rafi Ahmad",
    )
    resolver = _FakeTelegramResolver(
        RecipientResolution(RecipientResolutionStatus.RESOLVED, "Rafi Ahmad", contact=contact)
    )

    async def fake_plan(prompt):
        return {"plan": [_telegram_send_step()], "llm_provider": "dummy", "raw_prompt": prompt}

    async def fake_guarded(proposal, audit=None, traces=None, decision=None):
        guarded_calls.append((proposal, decision))
        return _event(proposal["run_id"], proposal["action_id"])

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "TelegramRecipientResolver", lambda: resolver)
    monkeypatch.setattr(agent_loop, "run_guarded_action", fake_guarded)

    run = run_registry.create("kirim pesan telegram 'halo' ke Rafi Ahmad")
    task = asyncio.create_task(agent_loop.run_agent_loop(run))
    await _wait_for(lambda: run.status == RunStatus.WAITING_APPROVAL)

    step = run.steps[0]
    assert step.decision["decision"] == Decision.NEED_APPROVAL
    assert step.data["payload"]["chat_id"] == 123456789
    assert step.data["recipient_reference"] == "Rafi Ahmad"
    assert step.data["resolved_recipient"]["username"] == "rafiahmad"
    assert "chat_id" not in step.public()["payload"]
    assert guarded_calls == []  # no connector/executor before approval

    run_registry.respond(run, 0, "approve")
    await asyncio.wait_for(task, timeout=5)

    assert run.status == RunStatus.DONE
    proposal, decision = guarded_calls[0]
    assert proposal["payload"]["chat_id"] == 123456789
    assert proposal["recipient_reference"] == "Rafi Ahmad"
    assert proposal["resolved_recipient"]["display_name"] == "Rafi Ahmad"
    assert decision.decision == Decision.ALLOW
    assert decision.initial_decision == Decision.NEED_APPROVAL
    assert decision.approval_decision == "approved"


@pytest.mark.asyncio
async def test_rejected_resolved_telegram_send_never_reaches_executor(monkeypatch):
    guarded_calls: list[dict] = []
    contact = TelegramContactIdentity(
        chat_id=123456789,
        chat_type="private",
        username="rafiahmad",
        first_name="Rafi",
        last_name="Ahmad",
        display_name="Rafi Ahmad",
    )
    resolver = _FakeTelegramResolver(
        RecipientResolution(RecipientResolutionStatus.RESOLVED, "Rafi Ahmad", contact=contact)
    )

    async def fake_plan(prompt):
        return {"plan": [_telegram_send_step()], "llm_provider": "dummy", "raw_prompt": prompt}

    async def fake_guarded(proposal, audit=None, traces=None, decision=None):
        guarded_calls.append(proposal)
        return _event(proposal["run_id"], proposal["action_id"])

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "TelegramRecipientResolver", lambda: resolver)
    monkeypatch.setattr(agent_loop, "run_guarded_action", fake_guarded)

    run = run_registry.create("kirim pesan telegram 'halo' ke Rafi Ahmad")
    task = asyncio.create_task(agent_loop.run_agent_loop(run))
    await _wait_for(lambda: run.status == RunStatus.WAITING_APPROVAL)
    run_registry.respond(run, 0, "decline")
    await asyncio.wait_for(task, timeout=5)

    assert run.status == RunStatus.DECLINED
    assert guarded_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "matches", "message"),
    [
        (RecipientResolutionStatus.NOT_FOUND, (), "belum terdaftar"),
        (RecipientResolutionStatus.AMBIGUOUS, (), "beberapa kontak"),
    ],
)
async def test_unresolved_or_ambiguous_telegram_recipient_asks_user_without_execution(
    monkeypatch, status, matches, message
):
    guarded_calls: list[dict] = []
    if status == RecipientResolutionStatus.AMBIGUOUS:
        matches = (
            TelegramContactIdentity(1, "private", "rafi_a", "Rafi", "Ahmad", "Rafi Ahmad"),
            TelegramContactIdentity(2, "private", "rafi_b", "Rafi", "Ahmad", "Rafi Ahmad"),
        )
    resolver = _FakeTelegramResolver(RecipientResolution(status, "Rafi Ahmad", matches=matches))

    async def fake_plan(prompt):
        return {"plan": [_telegram_send_step()], "llm_provider": "dummy", "raw_prompt": prompt}

    async def fake_guarded(proposal, audit=None, traces=None, decision=None):
        guarded_calls.append(proposal)
        return _event(proposal["run_id"], proposal["action_id"])

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "TelegramRecipientResolver", lambda: resolver)
    monkeypatch.setattr(agent_loop, "run_guarded_action", fake_guarded)

    run = run_registry.create("kirim pesan telegram 'halo' ke Rafi Ahmad")
    task = asyncio.create_task(agent_loop.run_agent_loop(run))
    await _wait_for(lambda: run.status == RunStatus.WAITING_INPUT)

    assert run.steps[0].decision["decision"] == Decision.ASK_USER
    assert message in run.steps[0].decision["reasons"][0]
    assert guarded_calls == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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
async def test_ask_user_clarification_then_execute(monkeypatch):
    """An ambiguous step (risk_hint in the ASK_USER set) pauses once for
    clarification, stores the answer, then executes."""
    browser_calls: list[dict] = []

    async def fake_plan(prompt):
        step = _open_step()
        step["risk_hint"] = "ambiguous_target"
        return {
            "plan": [step],
            "llm_provider": "dummy",
            "raw_prompt": prompt,
            "human_readable": "",
        }

    async def fake_browser(**kwargs):
        browser_calls.append(kwargs)
        return _event(kwargs["run_id"], kwargs["action_id"], data={"final_url": kwargs["url"]})

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent", fake_browser)

    run = run_registry.create("buka sesuatu")
    task = asyncio.create_task(agent_loop.run_agent_loop(run))
    await _wait_for(lambda: run.status == RunStatus.WAITING_INPUT)
    assert run.steps[0].status == StepStatus.WAITING_INPUT

    run_registry.respond(run, 0, "input", fields={"clarification": "buka github.com"})
    await asyncio.wait_for(task, timeout=5)

    assert run.status == RunStatus.DONE
    assert run.steps[0].status == StepStatus.DONE
    assert run.steps[0].clarified is True
    assert run.steps[0].data["user_clarification"] == "buka github.com"
    assert browser_calls[0]["run_id"] == run.run_id


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
            ATOMIC_BROWSER_AUDIT=False,
        ),
    )
    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "parse_next_steps", fake_replan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent", fake_browser)

    run = run_registry.create("loop")
    await asyncio.wait_for(agent_loop.run_agent_loop(run), timeout=5)

    assert len(run.steps) <= 3


@pytest.mark.asyncio
async def test_atomic_browser_audit_writes_one_row_per_step(monkeypatch):
    """When ATOMIC_BROWSER_AUDIT is on, a multi-step browser batch writes one
    AuditEvent per step (own action_id, shared run_id) via
    run_browser_prototype_agent_atomic, instead of a single combined event."""

    async def fake_plan(prompt):
        open_step = _open_step()
        click_step = {
            **_open_step(),
            "action_type": "BROWSER_CLICK",
            "payload": {"selector_hint": "login button"},
        }
        return {
            "plan": [open_step, click_step],
            "llm_provider": "dummy",
            "raw_prompt": prompt,
            "human_readable": "",
        }

    atomic_calls: list[dict] = []

    async def fake_browser_atomic(*, url, step_plan, settle_ms=0, run_id=None, navigate=True):
        atomic_calls.append({"url": url, "step_plan": step_plan})
        return [
            _event(request.run_id, request.action_id, data={"final_url": url})
            for _, request, _ in step_plan
        ]

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent_atomic", fake_browser_atomic)
    monkeypatch.setattr(
        agent_loop,
        "get_settings",
        lambda: SimpleNamespace(
            AGENT_MAX_STEPS=10,
            AGENT_MAX_REPLAN=2,
            AGENT_WAIT_RESPONSE_TIMEOUT_SEC=5.0,
            BROWSER_SETTLE_MS=0,
            GUARDRAIL_LLM_ENABLED=False,
            ATOMIC_BROWSER_AUDIT=True,
        ),
    )

    run = run_registry.create("open example and click login")
    await asyncio.wait_for(agent_loop.run_agent_loop(run), timeout=5)

    assert run.status == RunStatus.DONE
    assert len(atomic_calls) == 1
    step_plan = atomic_calls[0]["step_plan"]
    assert len(step_plan) == len(run.steps) == 2

    action_ids = {request.action_id for _, request, _ in step_plan}
    run_ids = {request.run_id for _, request, _ in step_plan}
    assert len(action_ids) == 2, "each step must get its own action_id"
    assert run_ids == {run.run_id}, "every step shares the same run_id"

    for step in run.steps:
        assert step.status == StepStatus.DONE
        assert step.audit_event is not None


@pytest.mark.asyncio
async def test_atomic_browser_audit_marks_remaining_steps_skipped_on_failure(monkeypatch):
    """If an action mid-batch fails, run_browser_prototype_agent_atomic is
    responsible for marking the rest SKIPPED; the loop just relays statuses."""

    async def fake_plan(prompt):
        return {
            "plan": [_open_step(), {**_open_step(), "action_type": "BROWSER_CLICK"}],
            "llm_provider": "dummy",
            "raw_prompt": prompt,
            "human_readable": "",
        }

    async def fake_browser_atomic(*, url, step_plan, settle_ms=0, run_id=None, navigate=True):
        first_request = step_plan[0][1]
        second_request = step_plan[1][1]
        return [
            _event(first_request.run_id, first_request.action_id, status=ExecutionStatus.FAILED),
            _event(second_request.run_id, second_request.action_id, status=ExecutionStatus.SKIPPED),
        ]

    monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
    monkeypatch.setattr(agent_loop, "run_browser_prototype_agent_atomic", fake_browser_atomic)
    monkeypatch.setattr(
        agent_loop,
        "get_settings",
        lambda: SimpleNamespace(
            AGENT_MAX_STEPS=10,
            AGENT_MAX_REPLAN=2,
            AGENT_WAIT_RESPONSE_TIMEOUT_SEC=5.0,
            BROWSER_SETTLE_MS=0,
            GUARDRAIL_LLM_ENABLED=False,
            ATOMIC_BROWSER_AUDIT=True,
        ),
    )

    run = run_registry.create("open example and click login")
    await asyncio.wait_for(agent_loop.run_agent_loop(run), timeout=5)

    assert run.steps[0].status == StepStatus.FAILED
    assert run.steps[1].status == StepStatus.SKIPPED
