# ADR 0004: Audit granularity and event sourcing

- **Status:** Proposed (needs a team decision)
- **Context:** Sprint 2 audit design v4 vs. the shipped implementation

## Context

The shipped audit is **action-sourced**: one immutable `audit_logs` row per action, `action_id` unique. Approval
outcome is stored as `initial_decision` and `approval_decision`. A separate `guardrail.jsonl` journal records each
guardrail evaluation. `ATOMIC_BROWSER_AUDIT` writes one row per browser step, otherwise one per batch.

Spec v4 proposes an append-only `audit_events` stream with `sequence_no` and explicit events for approval and
clarification. It improves replay, compliance metrics (F14/F16), and the approval timeline, but costs a new
schema, a new repository, migration of readers (`/runs`, `/audits`, `/actions`, `/benchmark`, `/approvals`) and
of the CLI's SQLite store.

## Relationship to existing ADRs

- **ADR 0002 (local-first CLI)** deliberately preserves "audit's final write-once-per-action contract" and adds a
  separate **execution intent journal** for approved operations (recorded before the external effect and again
  with the outcome). Any move to an event stream **amends that contract** and should say so; the intent journal
  already covers part of the timeline need for writes.
- **ADR 0003 (embedded guardrail)** added a per-evaluation **guardrail journal** that is separate from the final
  audit row. Together with the intent journal it already captures evaluation and execution-intent history
  outside `audit_logs`, so the question is narrower: whether the *audit table itself* should become an event stream.

## Options

1. **Keep action-sourced** (consistent with ADR 0002). Simple, already works with every reader, with the guardrail
   and intent journals covering evaluation and write-intent history. Weak on approval-request timing, ordering, replay.
2. **Add `audit_events` alongside `audit_logs`.** Write events during the loop; keep `audit_logs` as the
   summary. Readers migrate gradually. Two writes per action.
3. **Replace `audit_logs` with `audit_events`.** Cleanest model, largest migration; must rebuild derived views.

## Decision drivers

- Need to show *when* approval was requested and decided, and by whom.
- Approval pending at restart currently leaves no durable trace in chat runs.
- Immutability trigger must be carried to any new table.
- The unique `action_id` constraint blocks multiple rows per action.

## Suggested direction

Option 2: introduce `audit_events` with the same immutability trigger, emit events at approval, clarification,
sanitize, execution start/finish, and derive current state by fold. Keep `audit_logs` as a compatibility summary
until readers move. Decide `written_by` and `session_id` semantics (CLI has runs but no login/session concept).

## Consequences

Needs a migration `0005`, a repository with the same async interface plus `append_event`, tests for replay
ordering, and updated docs. Record the final choice here and change the status to Accepted.
