# Audit design: current vs. target

## Principles (both designs)

- Every action is audited: allowed, blocked, declined, failed, and pending ones.
- Audit records are immutable.
- Only sensitive entity **kinds** are stored. `ActionRequest.payload` is excluded from serialized audit records.

## Current implementation: action-sourced

**One row per action**, written once when the outcome is known.

| Aspect | Detail |
|--------|--------|
| Server table | `audit_logs` (migration `0001`): `audit_id` PK, `action_id` **unique**, `run_id` indexed, JSONB request/decision/execution |
| Immutability | Trigger `trg_audit_logs_immutable` rejects UPDATE and DELETE |
| Backends | `AuditRepositoryDB` (postgres) and `AuditRepository` (JSONL), same async interface: `write`, `latest`, `list`, `by_run`, `by_action`. `get_audit_repository()` picks by `AUDIT_BACKEND` (or the CLI runtime's SQLite store) |
| Guardrail journal | Separate `guardrail.jsonl`: one entry per **evaluation** (`guard_...` ID), including re-evaluations after input or redaction. Final audit rows reference it through `guardrail_audit_id` |
| Traces | `actions.jsonl` (`ActionTrace`), a model-ready export separate from audit. Written only by `run_guarded_action`, so browser batches and blocked/declined/timed-out chat steps have no trace |

### When rows are written

| Path | Row written | Row content |
|------|-------------|-------------|
| Chat run, `ALLOW` | After execution | `SUCCESS` / `FAILED` |
| Chat run, `NEED_APPROVAL` approved | After execution | Decision is `ALLOW` with `initial_decision=NEED_APPROVAL`, `approval_decision="approved"` |
| Chat run, declined | Immediately | `SKIPPED`, "declined by user", `approval_decision="declined"` |
| Chat run, `BLOCK` | Immediately | `BLOCKED` / `SKIPPED` |
| Chat run, timeout waiting | Immediately | `FAILED`, "timed out waiting for user response"; the run becomes `failed` and an `error` event is emitted |
| `POST /actions/run`, `NEED_APPROVAL` | Immediately | `PENDING_APPROVAL` snapshot (nothing resumes it) |
| `POST /actions/run`, `SANITIZE` / `ASK_USER` | Immediately | `SANITIZED` / `WAITING_USER` snapshot (nothing resumes it) |
| Browser batch (default) | After the batch | **One combined row** for consecutive browser steps sharing a URL |
| Browser batch, `ATOMIC_BROWSER_AUDIT=true` | Per step | One row per step (own `action_id`, shared `run_id`); after a mid-batch failure the remaining steps are `SKIPPED` |

Reading: the approval *outcome* is captured on the single row, but the *timeline* (when approval was requested,
when it was decided, who decided) is not stored as separate records.

### Consequences

- `action_id` uniqueness means an action can have only one row; a `PENDING_APPROVAL` snapshot from `/actions/run`
  cannot later be superseded by a second row for the same action.
- Intermediate states (sanitized preview, clarification exchange, approval request time) are visible only in the
  live run state and the guardrail journal, not in `audit_logs`.
- For declined, blocked, skipped and timed-out steps the loop **logs a warning and continues** if the audit write fails,
  so an audit failure there does not stop the run.

## Target: event-sourced (spec v4, `AgentGate_Sprint2_Audit_Schema_v4_ID.docx`)

An append-only `audit_events` table where each lifecycle step is its own event and state is derived by replaying
events ordered by `sequence_no`.

| Category | Events |
|----------|--------|
| Session | `SESSION_STARTED`, `SESSION_ENDED`, `PROMPT_SUBMITTED` |
| Agent / guardrail | `ACTION_CREATED`, `GUARDRAIL_EVALUATED`, `PAYLOAD_SANITIZED`, `ACTION_BLOCKED` |
| Human interaction | `USER_CLARIFICATION_REQUESTED`, `USER_CLARIFICATION_RECEIVED`, `APPROVAL_REQUESTED`, `APPROVAL_DECIDED`, `ACTION_REJECTED` |
| Execution | `EXECUTION_STARTED`, `EXECUTION_FINISHED`, `EXECUTION_FAILED`, `EXECUTION_SKIPPED` |

Envelope: `audit_event_id`, `session_id`, `action_id`, `event_type`, `timestamp`, `sequence_no`, `written_by`,
`domain`, `decision` (denormalized and indexed to keep Sprint 1 query patterns).

Derived states: `CREATED`, `UNDER_REVIEW`, `SANITIZED`, `AWAITING_USER`, `AWAITING_APPROVAL`, `APPROVED`,
`REJECTED`, `BLOCKED`, `EXECUTING`, `SUCCESS`, `FAILED`.

## Gap summary

| Topic | Spec v4 | Code today |
|-------|---------|------------|
| Table | `audit_events` (many rows per action) | `audit_logs` (one row per action) |
| Ordering | `sequence_no` per session | `created_at` |
| Approval history | `APPROVAL_REQUESTED` then `APPROVAL_DECIDED` | Outcome on the final row (`initial_decision`, `approval_decision`) |
| Skipped actions | Explicit `EXECUTION_SKIPPED` event | Status `SKIPPED` / `BLOCKED` on the row |
| Sanitize / clarification history | Separate events | Guardrail journal entries per evaluation |
| Session events | Defined | Not present |
| `decision`, `domain` columns | Denormalized, indexed | Inside JSONB, not indexed columns |
| Browser granularity | per action | per batch by default; per step with `ATOMIC_BROWSER_AUDIT` |

The guardrail journal plus `initial_decision` / `approval_decision` narrow the gap, but they do not give an
ordered, queryable event stream. Closing it requires a new migration and repository. The proposal is captured in
[ADR 0004](decisions/0004-audit-granularity.md).
