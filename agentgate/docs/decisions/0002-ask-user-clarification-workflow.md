# ADR 0002 — ASK_USER as a Clarification Workflow

- **Status:** Accepted
- **Date:** 2026-08
- **Context:** Redesigning ASK_USER from a confirmation gate into a true
  clarification workflow, while keeping `decide -> ExecutionRouter ->
  AuditRepository` unchanged.

## Decision

**ASK_USER and NEED_APPROVAL are different judgments, not two flavors of
"pause and ask a human":**

| | ASK_USER | NEED_APPROVAL |
|---|---|---|
| Guardrail has enough info to evaluate risk? | **No** | Yes |
| What's being asked for | Missing/clarifying information | Authorization to proceed with a known risk |
| Who can resolve it | The end user (may not know what "risk" even means) | A human reviewer |
| Can a response change *what* executes? | Yes -- `payload_updates` | No -- approve/reject the request as-is |

Concretely, in `app/domains/guardrail/decision/simple.py`: ASK_USER fires on
`confidence < threshold` and runs *before* the risk_hint check, because an
action can't be scored for risk until it's well-formed enough to evaluate.
NEED_APPROVAL only runs once that's already true.

**Resolving a clarification re-runs the normal decision pipeline; it does
not execute directly.** `clarification_service._resolve()` merges
`payload_updates` into the original payload (deep-merge, so nested fields
patch instead of clobbering siblings) and calls the same `decide()` used
by the primary flow. A correction can land on BLOCK, SANITIZE, or
NEED_APPROVAL exactly like a fresh proposal -- a bad correction never
executes just because the user said `proceed=true`.

**Confidence is only raised when the user actually supplied
`payload_updates`.** A bare `proceed=true` with nothing attached doesn't
resolve an information gap, so it doesn't force the decision through.
Concretely this means `decide()` can legitimately return `ASK_USER` again
on the corrected request -- which is a real state (`still_pending`), not
a bug to guard against with a hardcoded block.

**Multi-round clarification is bounded, not single-shot.** If the merged
proposal still doesn't clear the confidence threshold, the action goes
back into `pending_user_questions` for another round rather than failing
closed after exactly one. `clarification_round` (added in migration 0004)
tracks how many rounds an action has been through; `MAX_CLARIFICATION_ROUNDS`
(currently 3, in `clarification_service.py`) is the circuit breaker --
past that, the guardrail fails closed to BLOCK instead of asking forever.

**Only terminal outcomes are audited.** `pending_user_questions` is
working-queue state (mirrors `pending_approvals`, see the 0003 migration
comment) -- a given `action_id` lives in exactly one of
`{pending_user_questions, pending_approvals, audit_logs}` at a time.
The original ASK_USER and every `still_pending` re-ask are intermediate:
they mutate the queue and are never written to `audit_logs`.
`audit.write()` is called exactly once per action, at whichever terminal
outcome it eventually resolves to:

- `resolved` -- cancelled, executed, or blocked (including hitting the
  round limit)
- `escalated_to_approval` -- not itself terminal for auditing purposes,
  but ASK_USER's own involvement ends here; the approval flow audits it
  when a reviewer decides
- `expired` -- the window passed before the user responded

This is enforced by regression tests (`test_still_pending_never_calls_audit_write`,
`test_escalated_to_approval_never_calls_audit_write`,
`test_terminal_outcomes_write_audit_exactly_once`,
`test_initial_ask_user_never_calls_audit_write`), not just true by
construction.

**Workflow state belongs to the clarification domain, not the decision
engine.** `decide()` remains a pure function of `ActionRequest ->
DecisionResponse` with no knowledge of pending questions, rounds, or
merge history. All of that lives in `app/domains/clarification/` --
`PendingUserQuestion` (ORM row), `PendingUserQuestionRepository`
(persistence), `clarification_service` (orchestration). `simple.py` only
had to change its ASK_USER trigger's wording/policy tag to stop implying
a confirmation ("Should I proceed?") when it means a clarification
request ("what's missing?") -- the trigger condition itself
(`confidence < threshold`) didn't change.

## Consequences

- `decide -> ExecutionRouter -> AuditRepository` is untouched by any of
  this; `clarification_service` calls the same functions the primary
  pipeline does rather than special-casing execution.
- API callers must handle a fourth outcome (`still_pending`) from
  `POST /ask-user/{action_id}/respond`, not just resolved/escalated/expired.
  This is additive -- existing callers that only understood the first
  three outcomes still get correct behavior for those cases; they'd need
  updating to *also* handle a proposal that still isn't clear enough
  after one round, which is new capability, not a breaking change to
  existing round-1 behavior.
- Migration 0004 (`clarification_round`, default 1) is additive and
  backward-compatible with existing rows.
- There's still no structured "which fields are missing" signal in
  `ActionRequest` -- confidence is a single scalar proxy for "how well
  understood is this proposal." If the clarifying question needs to name
  specific missing fields rather than a generic confidence-gap message,
  that's a schema addition, not covered by this ADR.
