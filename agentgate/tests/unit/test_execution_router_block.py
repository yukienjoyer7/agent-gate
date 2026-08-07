import asyncio

from app.core.schemas import ActionRequest, Decision, DecisionResponse, ExecutionStatus, RiskLevel
from app.domains.guardrail.decision import decide
from app.executors.router import ExecutionRouter


def _blocked_action() -> ActionRequest:
    return ActionRequest(
        action_type="FILE_READ",
        target_system="local_file",
        target="secrets.py",
        risk_hint="source_code",
    )


def test_router_short_circuits_block_decision():
    action = _blocked_action()
    decision = decide(action)
    assert decision.decision == Decision.BLOCK  # sanity check on the fixture

    result = asyncio.run(ExecutionRouter().route(action, decision))

    assert result.status == ExecutionStatus.BLOCKED
    assert result.executor == "router"
    assert result.error is None


def test_router_never_calls_a_connector_for_block():
    """
    route() must return before touching api/browser executors -- a BLOCK
    decision should never result in a connector call, unlike NEED_APPROVAL
    (which is also short-circuited here, but can still execute later via
    ExecutionRouter.execute() once approved).
    """
    action = _blocked_action()
    decision = DecisionResponse(
        run_id=action.run_id,
        action_id=action.action_id,
        decision=Decision.BLOCK,
        risk_level=RiskLevel.CRITICAL,
        risk_score=1.0,
    )
    router = ExecutionRouter()

    async def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("connector should not be called for a BLOCK decision")

    router.api.execute = _fail_if_called
    router.browser.execute = _fail_if_called

    result = asyncio.run(router.route(action, decision))

    assert result.status == ExecutionStatus.BLOCKED
