# Runtime contracts (schema v0.1)

Source: `app/core/action_schema.py`, `executor_schema.py`, `audit_schema.py`, `browser_schema.py`,
`run_schema.py`, `errors.py`. All public models carry `schema_version = "0.1"`. IDs are `<prefix>_<12 hex>`
(`run_`, `act_`, `aud_`, `snap_`, `guard_`).

## Enums

| Enum | Values |
|------|--------|
| `Decision` | `ALLOW`, `BLOCK`, `NEED_APPROVAL`, `SANITIZE`, `ASK_USER` |
| `ExecutionStatus` | `SUCCESS`, `FAILED`, `SKIPPED`, `BLOCKED`, `PENDING_APPROVAL`, `SANITIZED`, `WAITING_USER` |
| `RiskLevel` | `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` |
| `RunStatus` (interactive run) | `running`, `waiting_approval`, `waiting_input`, `done`, `failed`, `blocked`, `declined`, `error`, `cancelled` |
| `StepStatus` (interactive step) | `pending`, `running`, `waiting_approval`, `waiting_input`, `approved`, `declined`, `done`, `failed`, `blocked`, `skipped` |
| `ConnectorErrorCode` | `AUTH`, `PERMISSION`, `RATE_LIMIT`, `TIMEOUT`, `VALIDATION`, `UNAVAILABLE`, `NOT_FOUND`, `UNKNOWN` |

`ExecutionStatus` describes the write-once audit outcome. `RunStatus`/`StepStatus` describe the live,
interactive lifecycle above it.

## ActionRequest

What the planner proposes.

| Field | Type | Notes |
|-------|------|-------|
| `run_id`, `action_id` | str | Generated if absent |
| `source` | str | Default `cli` |
| `domain` | str | `browser`, `productivity`, `code_protection`, `booking`, `filesystem`; defaults from `DOMAIN_BY_TARGET_SYSTEM` |
| `action_type` | str | `API_CALL`, `FILE_READ`, `BROWSER_OPEN`, `BROWSER_CLICK`, `BROWSER_TYPE`, ... |
| `target_system` | str | `local_file`, `github`, `gmail`, `calendar`, `telegram`, `stripe`, `browser` |
| `target` | str or object | Summary of the target |
| `recipient_reference`, `resolved_recipient` | str? / object? | Telegram recipient resolution, kept for audit |
| `user_goal` | str | The originating instruction |
| `content_context`, `payload_summary` | str | Short, redacted |
| `payload` | object | **Excluded from serialization**, so it never reaches the audit record |
| `browser_element` | `BrowserElement`? | `snapshot_id`, `element_id`, `role`, `label`, `text`, `risk_hint` |
| `risk_hint` | str | Default `unknown`; see [guardrail-policy](guardrail-policy.md) |
| `rollback_available` | bool | Default false |
| `confidence` | float 0-1 | Default 1.0 |
| `created_at` | datetime (UTC) | |

## DecisionResponse

| Field | Type | Notes |
|-------|------|-------|
| `decision` | `Decision` | Final verdict |
| `risk_level`, `risk_score` | enum, float 0-1 | |
| `reasons` | list[str] | Human-readable rationale |
| `triggered_policies` | list[str] | Policy rule IDs, e.g. `global.browser_submit` |
| `sensitive_entities` | list[str] | Entity **kinds** only, never raw values |
| `sanitized_payload` | object? | Redacted replacement, for `SANITIZE` (and approvals of redacted actions) |
| `initial_decision` | `Decision`? | Verdict before an approval/input step changed it |
| `approval_decision` | str? | Human outcome, when one occurred |
| `guardrail_audit_id` | str? | Links to the `guardrail.jsonl` evaluation journal entry (`guard_...`) |
| `evaluation_error` | str? | Set when a detector failed; forces approval |
| `next_step` | str | `execute`, `blocked`, `sanitize`, `ask_user`, `approval_queue` |
| `latency_ms` | int | |

## ExecutionResult

`run_id`, `action_id`, `executor`, `status` (`ExecutionStatus`), `result_summary`, `data` (object),
`error` (a serialized `ConnectorError` or null), `latency_ms`, `created_at`.

## ConnectorError

`code` (`ConnectorErrorCode`), `message`, `retryable` (bool), `details` (object).

## AuditEvent

One record per action (see [audit-design](audit-design.md)).

| Field | Notes |
|-------|-------|
| `audit_id` | `aud_...` |
| `run_id`, `action_id` | |
| `request_json`, `decision_json`, `execution_json` | Serialized models above |
| `execution_status` | Copied from the execution result |
| `error_type` | The error `code`, if any |
| `policy_version`, `detector_version` | Default `policy-0.1`, `detector-0.1` |
| `latency` | `guardrail_ms`, `executor_ms`, `audit_write_ms`, `total_ms`, ... |
| `created_at` | |

## ActionTrace

Model-ready export (JSONL), written only for actions run through `run_guarded_action`: `run_id`, `action_id`, `user_goal`, `raw_tool_call`, `action_request`, `decision`,
`execution`, `audit`, `latency`, `final_status`, `created_at`.
