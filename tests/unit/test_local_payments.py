import asyncio
import json

import pytest

from app.cli.main import main
from app.core.action_request import build_action_request
from app.core.schemas import Decision, DecisionResponse, ExecutionResult, ExecutionStatus
from app.runtime.config import LocalConfig, LocalPaths, save_config
from app.runtime.local import LazyConnector, LocalRuntime
from app.storage.local.database import LocalDatabase


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTGATE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGENTGATE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_unit_test_only_no_real_account")
    paths = LocalPaths.resolve()
    config = LocalConfig(
        workspace=str(tmp_path),
        credential_store="env",
        stripe_success_url="https://merchant.example/success",
        stripe_cancel_url="https://merchant.example/cancel",
        stripe_price_map={"demo": "price_demo"},
    )
    save_config(paths.config, config)
    return paths, config


def checkout(action_id="act_first"):
    return build_action_request(
        {
            "run_id": "run_test",
            "action_id": action_id,
            "action_type": "API_CALL",
            "target_system": "stripe",
            "target": "stripe",
            "payload": {"action": "create_checkout_session", "catalog_key": "demo", "quantity": 1},
        }
    )


def approved(request):
    return DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ALLOW,
        initial_decision=Decision.NEED_APPROVAL,
        approval_decision="approved",
    )


@pytest.mark.asyncio
async def test_checkout_intent_precedes_remote_call_and_replan_reuses_result(profile, monkeypatch):
    paths, config = profile
    calls = []

    async def execute(self, action, payload):
        calls.append(payload)
        with LocalDatabase(paths.database) as database:
            row = database.db.execute("SELECT * FROM intents").fetchone()
            assert row["state"] == "started"
            assert row["operation_key"].startswith("agentgate:run_test:checkout:")
        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="stripe",
            status=ExecutionStatus.SUCCESS,
            data={"id": "cs_test_demo", "status": "open", "payment_status": "unpaid"},
        )

    monkeypatch.setattr(LazyConnector, "execute", execute)
    with LocalRuntime(config, paths) as runtime:
        first = checkout()
        assert (await runtime.router.route(first, approved(first))).data["id"] == "cs_test_demo"
        retry = checkout("act_replanned")
        result = await runtime.router.route(retry, approved(retry))
        assert result.action_id == "act_replanned"
        assert len(calls) == 1
        assert runtime.payments.list()[0]["payment_status"] == "unpaid"


@pytest.mark.asyncio
async def test_unknown_checkout_blocks_replay_and_sync_recovers_known_id(
    profile, monkeypatch, capsys
):
    paths, config = profile
    calls = []

    async def failed(self, action, payload):
        calls.append(action)
        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="stripe",
            status=ExecutionStatus.FAILED,
            error={
                "code": "UNAVAILABLE",
                "retryable": True,
                "details": {"stripe_session_id": "cs_test_demo"},
            },
        )

    monkeypatch.setattr(LazyConnector, "execute", failed)
    with LocalRuntime(config, paths) as runtime:
        first = checkout()
        await runtime.router.route(first, approved(first))
        retry = checkout("act_retry")
        assert (
            await runtime.router.route(retry, approved(retry))
        ).status == ExecutionStatus.BLOCKED
        assert calls == ["create_checkout_session"]

    async def retrieved(self, action, payload):
        assert action == "retrieve_checkout_session"
        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="stripe",
            status=ExecutionStatus.SUCCESS,
            data={"id": "cs_test_demo", "status": "complete", "payment_status": "paid"},
        )

    monkeypatch.setattr(LazyConnector, "execute", retrieved)
    assert await asyncio.to_thread(main, ["payments", "sync", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["unresolved"] == []
    assert result["payments"][0]["payment_status"] == "paid"


@pytest.mark.asyncio
async def test_live_mode_financial_request_is_blocked(profile, monkeypatch):
    paths, config = profile
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_not_allowed")
    with LocalRuntime(config, paths) as runtime:
        request = checkout()
        assert (
            await runtime.router.route(request, approved(request))
        ).status == ExecutionStatus.BLOCKED
        assert runtime.database.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0


def test_unknown_without_remote_id_is_reported_not_retried(profile, capsys):
    paths, config = profile
    with LocalRuntime(config, paths) as runtime:
        request = checkout()
        key, _ = runtime.router.intents.begin(request, "unknown-operation")
        runtime.router.intents.unknown(key)
    assert main(["payments", "sync", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["unresolved"][0]["operation_key"] == "unknown-operation"


@pytest.mark.asyncio
async def test_equivalent_refund_replan_ignores_key_order_and_unused_fields(profile, monkeypatch):
    paths, config = profile
    calls = []

    async def execute(self, action, payload):
        calls.append(payload)
        return ExecutionResult(
            run_id=payload["run_id"],
            action_id=payload["action_id"],
            executor="stripe",
            status=ExecutionStatus.SUCCESS,
            data={"id": "re_demo", "status": "succeeded"},
        )

    monkeypatch.setattr(LazyConnector, "execute", execute)
    with LocalRuntime(config, paths) as runtime:
        proposal = {
            "run_id": "run_refund",
            "action_id": "act_first",
            "action_type": "API_CALL",
            "target_system": "stripe",
            "target": "stripe",
            "payload": {
                "action": "create_refund",
                "payment_intent_id": "pi_demo",
                "amount": 100,
            },
        }
        first = build_action_request(proposal)
        await runtime.router.route(first, approved(first))
        proposal.update(
            action_id="act_replanned",
            target="different description",
            payload={
                "amount": 100,
                "reason": None,
                "action": "create_refund",
                "payment_intent_id": "pi_demo",
                "unused_model_field": "ignored",
            },
        )
        retry = build_action_request(proposal)
        assert (await runtime.router.route(retry, approved(retry))).data["id"] == "re_demo"
        assert len(calls) == 1
