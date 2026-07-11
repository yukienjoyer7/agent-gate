"""
Multi-scenario demo for AuditRepositoryDB (action-sourced, Postgres).

Run: uv run python scripts/audit_db_prototype_demo.py
Requires DATABASE_URL set (env var or .env) and migration 0001 applied:
  uv run alembic upgrade head

Covers 4 scenarios, each writing exactly ONE row to audit_logs (matching
the current guarded_execution.py behavior -- see conversation notes on the
NEED_APPROVAL row being a snapshot, not a final resumed state):

  1. ALLOW         -> executed, SUCCESS
  2. BLOCK          -> never executed, status BLOCKED (via router.skipped())
  3. NEED_APPROVAL  -> never executed, status PENDING_APPROVAL (via skipped())
  4. ALLOW          -> executed, but connector fails -> FAILED

After writing all 4, runs each query method (by_action, by_run, list,
latest) so you can see what each returns.
"""

import asyncio
import sys

if sys.platform == "win32":
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop.
    # Must be set before any event loop is created (i.e. before asyncio.run()).
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.core.action_schema import ExecutionStatus, RiskLevel
from app.core.schemas import ActionRequest, Decision, DecisionResponse, ExecutionResult
from app.domains.audit.repositories.audit_repository_db import AuditRepositoryDB


def make_request(**overrides) -> ActionRequest:
    defaults = dict(
        source="cli",
        domain="productivity",
        action_type="send_email",
        target_system="gmail",
        target="finance-team-distro",
        payload_summary="Send Q2 budget summary with attachment",
        payload={"body": "...", "attachment": "q2_summary.pdf"},
        risk_hint="external_send",
        confidence=0.87,
    )
    defaults.update(overrides)
    return ActionRequest(**defaults)


async def scenario_allow_success(repo: AuditRepositoryDB):
    """ALLOW -> executor runs -> SUCCESS."""
    request = make_request(
        action_type="list_files",
        target_system="local_file",
        target="demo_data/",
        risk_hint="unknown",
    )
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        risk_score=0.05,
        reasons=["risk_hint=unknown"],
        next_step="execute",
        latency_ms=12,
    )
    execution = ExecutionResult(
        run_id=request.run_id,
        action_id=request.action_id,
        executor="local_file",
        status=ExecutionStatus.SUCCESS,
        result_summary="Listed 4 files",
        latency_ms=8,
    )
    event = await repo.write(request, decision, execution)
    print(f"[1] ALLOW/SUCCESS       action_id={event.action_id} status={event.execution_status}")
    return event


async def scenario_block(repo: AuditRepositoryDB):
    """BLOCK -> guardrail stops it -> never reaches executor."""
    request = make_request(
        action_type="delete_repo",
        target_system="github",
        target="agent-gate",
        risk_hint="destructive",
    )
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.BLOCK,
        risk_level=RiskLevel.CRITICAL,
        risk_score=0.98,
        reasons=["risk_hint=destructive"],
        triggered_policies=["destructive_action_blocked"],
        next_step="blocked",
        latency_ms=9,
    )
    execution = ExecutionResult(
        run_id=request.run_id,
        action_id=request.action_id,
        executor="router",
        status=ExecutionStatus.BLOCKED,
        result_summary="blocked by guardrail",
    )
    event = await repo.write(request, decision, execution)
    print(f"[2] BLOCK               action_id={event.action_id} status={event.execution_status}")
    return event


async def scenario_need_approval(repo: AuditRepositoryDB):
    """
    NEED_APPROVAL -> router returns PENDING_APPROVAL immediately, no
    executor call. NOTE (per our discussion): this row is written NOW, at
    this snapshot -- guarded_execution.py does not currently wait for a
    reviewer decision before calling write(), and there is no
    approve/reject endpoint yet that would update this row afterward.
    action_id is UNIQUE in audit_logs, so a second write() for the same
    action_id once approval flow exists needs a different strategy (not
    solved here -- out of scope for this prototype).
    """
    request = make_request()
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.NEED_APPROVAL,
        risk_level=RiskLevel.MEDIUM,
        risk_score=0.62,
        reasons=["risk_hint=external_send"],
        triggered_policies=["external_recipient_email", "financial_data_disclosure"],
        sensitive_entities=["FINANCIAL_FIGURE"],
        next_step="approval_queue",
        latency_ms=288,
    )
    execution = ExecutionResult(
        run_id=request.run_id,
        action_id=request.action_id,
        executor="router",
        status=ExecutionStatus.PENDING_APPROVAL,
        result_summary="pending approval",
    )
    event = await repo.write(request, decision, execution)
    print(f"[3] NEED_APPROVAL       action_id={event.action_id} status={event.execution_status}")
    return event


async def scenario_allow_failed(repo: AuditRepositoryDB):
    """ALLOW -> executor runs -> connector raises -> FAILED."""
    request = make_request(
        action_type="create_issue",
        target_system="github",
        target="agent-gate#issues",
        risk_hint="unknown",
    )
    decision = DecisionResponse(
        run_id=request.run_id,
        action_id=request.action_id,
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        risk_score=0.1,
        reasons=["risk_hint=unknown"],
        next_step="execute",
        latency_ms=15,
    )
    execution = ExecutionResult(
        run_id=request.run_id,
        action_id=request.action_id,
        executor="github",
        status=ExecutionStatus.FAILED,
        result_summary="GitHub API returned 401",
        error={"code": "AUTH_ERROR", "message": "invalid or expired token"},
        latency_ms=340,
    )
    event = await repo.write(request, decision, execution)
    print(
        f"[4] ALLOW/FAILED        action_id={event.action_id} status={event.execution_status} "
        f"error_type={event.error_type}"
    )
    return event


async def show_queries(repo: AuditRepositoryDB, events: list) -> None:
    print("\n--- queries ---")

    latest = await repo.latest()
    print(f"latest()                 -> audit_id={latest.audit_id} action_id={latest.action_id}")

    all_events = await repo.list()
    print(f"list()                   -> {len(all_events)} row(s) total")

    target = events[0]
    by_action = await repo.by_action(target.action_id)
    print(f"by_action(scenario 1 id) -> status={by_action.execution_status}")

    by_run = await repo.by_run(target.run_id)
    print(f"by_run(scenario 1 run)   -> {len(by_run)} row(s) "
          f"(each scenario uses its own run_id here, so this is 1)")


async def main() -> None:
    repo = AuditRepositoryDB()  # self-manages its own session per call

    events = []
    for scenario in (
        scenario_allow_success,
        scenario_block,
        scenario_need_approval,
        scenario_allow_failed,
    ):
        events.append(await scenario(repo))

    await show_queries(repo, events)


if __name__ == "__main__":
    asyncio.run(main())