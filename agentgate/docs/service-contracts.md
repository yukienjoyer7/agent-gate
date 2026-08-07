# AgentGate Service Contracts

## Runtime Chain

Every guarded action produces:

```text
raw_tool_call -> ActionRequest -> DecisionResponse -> ExecutionResult -> AuditEvent -> ActionTrace
```

All public records include `schema_version`, `run_id`, and `action_id` where applicable.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/runs` | List runs with action counts and latest status |
| GET | `/api/v1/runs/{run_id}/actions` | List audit events for one run |
| GET | `/api/v1/actions/{action_id}` | Inspect one action chain |
| GET | `/api/v1/audits` | List audit events, optional `run_id` filter |
| GET | `/api/v1/audits/latest` | Return latest audit event |
| GET | `/api/v1/approvals` | Return pending approval events |
| POST | `/api/v1/approvals/{action_id}/decide` | Approve or reject a pending action |
| GET | `/api/v1/ask-user` | Return pending clarification questions |
| POST | `/api/v1/ask-user/{action_id}/respond` | Clarify/complete or cancel a proposal; see below |
| GET | `/api/v1/benchmark` | Return current latency summary |

### POST /api/v1/ask-user/{action_id}/respond

ASK_USER means the guardrail doesn't have enough information/confidence to
evaluate the proposal at all -- this is a clarification workflow, not a
confirmation gate. The response can supply the missing information; it
isn't just a yes/no on the original (possibly incomplete) proposal.

**Request body** (`UserResponseRequest`):

| Field | Type | Meaning |
|---|---|---|
| `proceed` | bool | `false` cancels the action outright. `true` means "evaluate this" -- see `payload_updates` below for what "this" is. |
| `payload_updates` | dict \| null | Fields to merge into the original proposal's payload (deep-merged: nested dict fields are patched key-by-key, not replaced wholesale). Omit or send `{}` if nothing needs correcting. |

Whatever the merged result is, it goes back through the **same** `decide()`
pipeline the original proposal went through -- not a direct execute. A
correction can still land on BLOCK, SANITIZE, or NEED_APPROVAL instead of
running.

**Response** -- one of four outcomes, distinguished by `outcome` /
`execution_status`:

| Outcome | When | Written to `audit_logs`? |
|---|---|---|
| `resolved` | Cancelled, or re-decided to ALLOW (executed)/BLOCK | Yes -- exactly once, this call |
| `escalated_to_approval` | Corrected proposal still exceeds the autonomous-risk threshold | No -- moves to `pending_approvals`, audited when a reviewer decides |
| `still_pending` | `proceed=true` but confidence is still below threshold (e.g. no `payload_updates` supplied, or the correction didn't resolve it) | No -- back in `pending_user_questions` for another round |
| `expired` (HTTP 410) | The ask-user window passed before this call | Yes -- audited as EXPIRED |

`still_pending` is bounded by `MAX_CLARIFICATION_ROUNDS` (currently 3); once
exhausted, the action fails closed to BLOCK (a `resolved` outcome) rather
than asking forever.

Only `resolved` and `expired` ever produce an audit row. Intermediate
clarification requests -- the original ASK_USER and any `still_pending`
re-asks -- are working-queue state in `pending_user_questions`, not audit
events; see ADR 0002.

## CLI

```bash
python scripts/run_demo_scenario.py local_file_read
python scripts/run_demo_scenario.py browser_snapshot
python scripts/run_demo_scenario.py payload_sanitize
python scripts/run_demo_scenario.py ask_user_low_confidence
python scripts/export_audit.py --latest
python scripts/export_traces.py --latest
```

Audit JSONL defaults to `artifacts/audit/events.jsonl`.
Trace JSONL defaults to `artifacts/traces/actions.jsonl`.
