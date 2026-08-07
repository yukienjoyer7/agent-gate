"""
Multi-scenario demo for AuditRepositoryDB (action-sourced, Postgres),
driven through the REAL pipeline -- run_guarded_action() -> decide() ->
ExecutionRouter -> audit repository, and decide_pending_approval() for
the approval lifecycle -- instead of hand-building
ActionRequest/DecisionResponse/ExecutionResult and calling
AuditRepositoryDB.write() directly. The old hand-built version could not
reveal whether decide(), the router, or the pending-approval flow
actually produced these rows; this one proves they do.

Run: uv run python scripts/audit_db_prototype_demo.py
Requires DATABASE_URL set (env var or .env) and migrations applied:
  uv run alembic upgrade head
Requires AUDIT_BACKEND=postgres (this is the settings default; override
via .env if your environment has it set to "jsonl").

SCOPE NOTE: app/domains/guardrail/decision/simple.py returns Decision.ALLOW,
Decision.NEED_APPROVAL, Decision.BLOCK (risk_hint="source_code" is
hard-blocked -- see BLOCKED_RISK_HINTS in that module), Decision.SANITIZE
(sensitive-entity scan on the payload -- see
app/domains/guardrail/detectors/sensitive_entities.py), or Decision.ASK_USER
(confidence below settings.ASK_USER_CONFIDENCE_THRESHOLD -- see
app/domains/clarification/services/clarification_service.py). So this
script covers every ExecutionStatus that's actually reachable through the
real pipeline today:

  1. ALLOW                          -> executed, SUCCESS
  2. ALLOW                          -> executed, connector fails, FAILED
  3. NEED_APPROVAL                  -> queued in pending_approvals,
                                        NOT written to audit_logs yet
     3a. ... APPROVED (decide_pending_approval) -> executed -> SUCCESS
     3b. ... APPROVED, but the connector itself fails -> FAILED
     3c. ... REJECTED                            -> audit_logs row, REJECTED
     3d. ... left past its TTL, resolved on decide -> audit_logs row, EXPIRED
  4. BLOCK                          -> never queued, never executed --
                                        audit_logs row written immediately,
                                        BLOCKED
  5. SANITIZE -> ALLOW              -> payload redacted in-process, decide()
                                        re-run on the clean payload, executes
                                        and writes a normal SUCCESS row --
                                        the sanitize history lives in
                                        decision_json.reasons /
                                        triggered_policies, not a separate
                                        row (see guarded_execution.py)
  6. ASK_USER                       -> queued in pending_user_questions,
                                        NOT written to audit_logs yet
     6a. ... proceed=True + payload_updates supplying the missing field
             (decide_pending_question) -> re-decided, executed -> SUCCESS
     6b. ... proceed=False                       -> audit_logs row, CANCELLED
     6c. ... proceed=True + payload_updates, but the completed proposal is
             still risky by risk_hint -> escalated to pending_approvals,
             NOT written to audit_logs
     6d. ... left past its TTL, resolved on respond -> audit_logs row, EXPIRED

SKIPPED is not exercised here since nothing in the current decision engine
can produce it.

After writing everything, runs each audit query method (by_action, by_run,
list, latest) so you can see what each returns.
"""

import asyncio
import sys

if sys.platform == "win32":
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop.
    # Must be set before any event loop is created (i.e. before asyncio.run()).
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.core.action_request import build_action_request
from app.domains.agent.services import run_guarded_action
from app.domains.approval.repositories.pending_approval_repository import PendingApprovalRepository
from app.domains.approval.schemas.pending_approval import ApprovalDecision, PendingApprovalResponse
from app.domains.approval.services import decide_pending_approval
from app.domains.audit.repositories import get_audit_repository
from app.domains.clarification.repositories import PendingUserQuestionRepository
from app.domains.clarification.schemas.pending_user_question import UserResponseRequest
from app.domains.clarification.services import decide_pending_question
from app.domains.guardrail.decision import decide


async def scenario_allow_success(repo) -> str:
    """ALLOW -> real executor runs -> SUCCESS."""
    proposal = {
        "source": "cli",
        "domain": "filesystem",
        "action_type": "list_files",
        "target_system": "local_file",
        "target": "sample.txt",
        "risk_hint": "unknown",
        "payload": {"action": "read", "path": "sample.txt"},
    }
    event = await run_guarded_action(proposal, audit=repo)
    print(f"[1] ALLOW/SUCCESS       action_id={event.action_id} status={event.execution_status}")
    return event.action_id


async def scenario_allow_failed(repo) -> None:
    """ALLOW -> real executor runs -> file not found -> FAILED."""
    proposal = {
        "source": "cli",
        "domain": "filesystem",
        "action_type": "list_files",
        "target_system": "local_file",
        "target": "does_not_exist.txt",
        "risk_hint": "unknown",
        "payload": {"action": "read", "path": "does_not_exist.txt"},
    }
    event = await run_guarded_action(proposal, audit=repo)
    print(f"[2] ALLOW/FAILED        action_id={event.action_id} status={event.execution_status}")


def _bulk_archive_proposal() -> dict:
    """
    risk_hint="bulk_action" -> decide() returns NEED_APPROVAL. Gmail's
    connector is a mock that always succeeds, so this is the proposal
    used for the "approved -> SUCCESS" and "rejected" scenarios.
    """
    return {
        "source": "cli",
        "domain": "productivity",
        "action_type": "gmail_archive",
        "target_system": "gmail",
        "target": "inbox",
        "risk_hint": "bulk_action",
        "payload": {"action": "archive", "query": "older_than:30d", "affected_items": 320},
    }


def _destructive_github_proposal() -> dict:
    """
    risk_hint="destructive" -> decide() returns NEED_APPROVAL. GitHubConnector
    only supports the "repo_metadata" connector action; anything else fails
    deterministically with no network call, which is what makes this the
    proposal for the "approved -> connector still fails -> FAILED" scenario.
    """
    return {
        "source": "cli",
        "domain": "code_protection",
        "action_type": "delete_repo",
        "target_system": "github",
        "target": "agent-gate",
        "risk_hint": "destructive",
        "payload": {"action": "delete_repo", "repo": "agent-gate"},
    }


async def scenario_need_approval_pending(repo) -> None:
    """NEED_APPROVAL -> queued, nothing in audit_logs yet."""
    pending = await run_guarded_action(_bulk_archive_proposal(), audit=repo)
    assert isinstance(pending, PendingApprovalResponse)
    print(f"[3] NEED_APPROVAL       action_id={pending.action_id} (queued, no audit row yet)")


async def scenario_need_approval_then_approved_success(repo, pending_repo) -> None:
    """NEED_APPROVAL -> APPROVE -> connector succeeds -> audit_logs row, SUCCESS."""
    pending = await run_guarded_action(_bulk_archive_proposal(), audit=repo)
    assert isinstance(pending, PendingApprovalResponse)

    outcome, event = await decide_pending_approval(
        pending.action_id, ApprovalDecision.APPROVE, repo=pending_repo, audit=repo
    )
    print(
        f"[3a] -> APPROVED/SUCCESS action_id={event.action_id} status={event.execution_status} "
        f"outcome={outcome}"
    )


async def scenario_need_approval_then_approved_failed(repo, pending_repo) -> None:
    """NEED_APPROVAL -> APPROVE -> connector itself fails -> audit_logs row, FAILED."""
    pending = await run_guarded_action(_destructive_github_proposal(), audit=repo)
    assert isinstance(pending, PendingApprovalResponse)

    outcome, event = await decide_pending_approval(
        pending.action_id, ApprovalDecision.APPROVE, repo=pending_repo, audit=repo
    )
    print(
        f"[3b] -> APPROVED/FAILED  action_id={event.action_id} status={event.execution_status} "
        f"outcome={outcome}"
    )


async def scenario_need_approval_then_rejected(repo, pending_repo) -> None:
    """NEED_APPROVAL -> REJECT -> audit_logs row, REJECTED."""
    pending = await run_guarded_action(_bulk_archive_proposal(), audit=repo)
    assert isinstance(pending, PendingApprovalResponse)

    outcome, event = await decide_pending_approval(
        pending.action_id, ApprovalDecision.REJECT, repo=pending_repo, audit=repo
    )
    print(f"[3c] -> REJECTED        action_id={event.action_id} status={event.execution_status} outcome={outcome}")


async def scenario_need_approval_then_expired(repo, pending_repo) -> None:
    """
    NEED_APPROVAL -> queued with a TTL that's already elapsed -> resolved
    as EXPIRED the moment decide_pending_approval() looks at it, the same
    check list_pending_approvals()/_finalize_expired() perform for the
    lazy-expiry path. ttl_minutes=-1 makes this deterministic without
    sleeping for the real APPROVAL_TTL_MINUTES or touching the DB row by
    hand.
    """
    request = build_action_request(_bulk_archive_proposal())
    decision = decide(request)
    row = await pending_repo.create(request, decision, ttl_minutes=-1)

    outcome, event = await decide_pending_approval(
        row.action_id, ApprovalDecision.APPROVE, repo=pending_repo, audit=repo
    )
    print(f"[3d] -> EXPIRED         action_id={event.action_id} status={event.execution_status} outcome={outcome}")


async def scenario_sanitize(repo) -> None:
    """
    SANITIZE -> redacted in-process by guarded_execution.py's re-evaluation
    loop -> re-decided as ALLOW -> executed -> SUCCESS. Payload carries an
    email address, which app/domains/guardrail/detectors/sensitive_entities.py
    detects; risk_hint itself is low-risk ("file_read"), so once the payload
    is clean the second decide() pass falls through to ALLOW, not
    NEED_APPROVAL. The sanitize history (which entity types were found, that
    sanitization happened at all) is folded into decision_json.reasons and
    decision_json.triggered_policies on the final row -- there is no
    separate PAYLOAD_SANITIZED audit row, by design (see guarded_execution.py).
    """
    proposal = {
        "source": "cli",
        "domain": "filesystem",
        "action_type": "FILE_READ",
        "target_system": "local_file",
        "target": "sample.txt",
        "risk_hint": "file_read",
        "payload": {
            "action": "read",
            "path": "sample.txt",
            "note": "flag any issues to reviewer@example.com",
        },
    }
    event = await run_guarded_action(proposal, audit=repo)
    print(
        f"[5] SANITIZE/SUCCESS    action_id={event.action_id} status={event.execution_status} "
        f"sanitized={'payload_sanitization_required' in event.decision_json['triggered_policies']}"
    )


async def scenario_block(repo) -> None:
    """
    BLOCK -> never queued, never executed -> audit_logs row written
    immediately with status BLOCKED. Unlike NEED_APPROVAL, there's no
    PendingApprovalResponse here: run_guarded_action() returns an AuditEvent
    directly, same shape as the ALLOW scenarios, just with a different
    execution_status and no connector call underneath it.
    """
    proposal = {
        "source": "cli",
        "domain": "code_protection",
        "action_type": "FILE_READ",
        "target_system": "github",
        "target": "agent-gate/app/config/settings.py",
        "risk_hint": "source_code",
        "payload": {"action": "read", "path": "app/config/settings.py"},
    }
    event = await run_guarded_action(proposal, audit=repo)
    print(f"[4] BLOCK               action_id={event.action_id} status={event.execution_status}")


def _incomplete_file_read_proposal(risk_hint: str = "file_read") -> dict:
    """
    action_type="FILE_READ" IS registered in REQUIRED_FIELDS
    (app/domains/clarification/rules.py, requires "path"). Per simple.py,
    once an action_type is registered, completeness is the *only* signal
    decide() consults for it -- confidence is not consulted at all, so a
    proposal like this with "path" already present would just fall
    through to ALLOW/NEED_APPROVAL regardless of any confidence value
    passed in. "path" is deliberately omitted here so decide() actually
    hits the completeness branch and returns ASK_USER -- confidence is
    not part of this at all for a registered action_type.
    """
    return {
        "source": "cli",
        "domain": "filesystem",
        "action_type": "FILE_READ",
        "target_system": "local_file",
        "target": "sample.txt",
        "risk_hint": risk_hint,
        "payload": {"action": "read"},  # "path" deliberately missing
    }


async def scenario_ask_user_pending(repo) -> None:
    """ASK_USER -> queued, nothing in audit_logs yet."""
    pending = await run_guarded_action(_incomplete_file_read_proposal(), audit=repo)
    print(f"[6] ASK_USER            action_id={pending.action_id} (queued, no audit row yet)")


async def scenario_ask_user_then_confirmed_success(repo, question_repo) -> None:
    """
    ASK_USER -> proceed=True + payload_updates supplying the missing
    "path" -> re-decided on the merged payload, executes -> audit_logs
    row, SUCCESS. proceed=True alone (no payload_updates) would leave the
    payload identical and decide() would legitimately return ASK_USER
    again -- per clarification_service's invariant that only the proposal
    changing, not the boolean itself, can move the decision.
    """
    pending = await run_guarded_action(_incomplete_file_read_proposal(), audit=repo)

    # DIAGNOSTIC: confirm the row is actually visible via a fresh session
    # before decide_pending_question tries to claim it. If this print shows
    # None, the row never made it into pending_user_questions in the first
    # place (a create()/commit problem) rather than a claim() problem.
    check = await question_repo.get(pending.action_id)
    print(f"[6a-debug] get({pending.action_id}) -> {'FOUND' if check else 'NOT FOUND'}")

    outcome, event = await decide_pending_question(
        pending.action_id,
        UserResponseRequest(proceed=True, payload_updates={"path": "sample.txt"}),
        repo=question_repo,
        audit=repo,
    )
    print(
        f"[6a] -> CONFIRMED/SUCCESS action_id={event.action_id} status={event.execution_status} "
        f"outcome={outcome}"
    )


async def scenario_ask_user_then_cancelled(repo, question_repo) -> None:
    """ASK_USER -> proceed=False -> audit_logs row, CANCELLED."""
    pending = await run_guarded_action(_incomplete_file_read_proposal(), audit=repo)

    outcome, event = await decide_pending_question(
        pending.action_id, UserResponseRequest(proceed=False), repo=question_repo, audit=repo
    )
    print(f"[6b] -> CANCELLED        action_id={event.action_id} status={event.execution_status} outcome={outcome}")


async def scenario_ask_user_then_escalated_to_approval(repo, question_repo) -> None:
    """
    ASK_USER -> proceed=True + payload_updates supplying "path", but
    risk_hint="external_send" makes the re-decide come back NEED_APPROVAL
    instead of ALLOW once the proposal is complete -- confirming that
    completing an ASK_USER proposal doesn't skip the approval check, it
    just clears the completeness gate. Handed off to pending_approvals;
    still nothing in audit_logs.
    """
    pending = await run_guarded_action(
        _incomplete_file_read_proposal(risk_hint="external_send"), audit=repo
    )

    outcome, pending_approval = await decide_pending_question(
        pending.action_id,
        UserResponseRequest(proceed=True, payload_updates={"path": "sample.txt"}),
        repo=question_repo,
        audit=repo,
    )
    print(
        f"[6c] -> ESCALATED        action_id={pending_approval.action_id} outcome={outcome} "
        "(queued in pending_approvals, no audit row yet)"
    )


async def scenario_ask_user_then_expired(repo, question_repo) -> None:
    """
    ASK_USER -> queued with a TTL that's already elapsed -> resolved as
    EXPIRED the moment decide_pending_question() looks at it, the same
    lazy-expiry check list_pending_questions()/_finalize_expired() perform.
    ttl_minutes=-1 makes this deterministic without sleeping for the real
    ASK_USER_TTL_MINUTES or touching the DB row by hand -- same trick as
    scenario_need_approval_then_expired above.
    """
    request = build_action_request(_incomplete_file_read_proposal())
    decision = decide(request)
    row = await question_repo.create(request, decision, ttl_minutes=-1)

    outcome, event = await decide_pending_question(
        row.action_id,
        UserResponseRequest(proceed=True, payload_updates={"path": "sample.txt"}),
        repo=question_repo,
        audit=repo,
    )
    print(f"[6d] -> EXPIRED          action_id={event.action_id} status={event.execution_status} outcome={outcome}")


async def show_queries(repo, first_action_id: str) -> None:
    print("\n--- queries ---")

    latest = await repo.latest()
    print(f"latest()                 -> audit_id={latest.audit_id} action_id={latest.action_id}")

    all_events = await repo.list()
    print(f"list()                   -> {len(all_events)} row(s) total")

    by_action = await repo.by_action(first_action_id)
    print(f"by_action(scenario 1 id) -> status={by_action.execution_status}")

    by_run = await repo.by_run(by_action.run_id)
    print(f"by_run(scenario 1 run)   -> {len(by_run)} row(s)")


async def main() -> None:
    repo = get_audit_repository()
    pending_repo = PendingApprovalRepository()
    question_repo = PendingUserQuestionRepository()

    first_action_id = await scenario_allow_success(repo)
    await scenario_allow_failed(repo)
    await scenario_need_approval_pending(repo)
    await scenario_need_approval_then_approved_success(repo, pending_repo)
    await scenario_need_approval_then_approved_failed(repo, pending_repo)
    await scenario_need_approval_then_rejected(repo, pending_repo)
    await scenario_need_approval_then_expired(repo, pending_repo)
    await scenario_block(repo)
    await scenario_sanitize(repo)
    await scenario_ask_user_pending(repo)
    await scenario_ask_user_then_confirmed_success(repo, question_repo)
    await scenario_ask_user_then_cancelled(repo, question_repo)
    await scenario_ask_user_then_escalated_to_approval(repo, question_repo)
    await scenario_ask_user_then_expired(repo, question_repo)

    await show_queries(repo, first_action_id)


if __name__ == "__main__":
    asyncio.run(main())
