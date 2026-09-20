# Guardrail policy

How AgentGate turns a proposed action into a verdict. Source: `app/domains/guardrail/`. Engine details:
[integrations/guardrail.md](integrations/guardrail.md). Why it is embedded:
[ADR 0003](decisions/0003-embedded-guardrail-engine.md).

The engine is a private vendored copy of the upstream `NafisNaufal/agentgate` core, pinned at revision
`9b919c604d2a0955fff26b4f05045281514c0220`. The application adapts its own action contract and trusted connector
metadata to it and maps the result back with existing policy floors. Every evaluation is recorded in a durable
guardrail journal **before** it reaches execution; if that record cannot be written, the action does not run.

## Backends

| `GUARDRAIL_BACKEND` | Behaviour |
|---------------------|-----------|
| `agentgate` (default) | Embedded upstream engine: six Ollama detectors + policy packs + scoring, combined with the application rule engine |
| `legacy` | Deterministic rules only, plus an optional LLM second opinion (`GUARDRAIL_LLM_ENABLED`). Must be selected explicitly; there is no automatic fallback |

## Verdict resolution (`agentgate` backend)

Two verdicts are computed and merged: the **upstream engine** (detectors + policy packs) and the
**application rules** (a minimum floor from trusted connector metadata). The merge order is:

1. `BLOCK` wins over everything.
2. `ASK_USER` next: approving an action whose intent is unknown is not meaningful.
3. A detector failure (`evaluation_error`) forces `NEED_APPROVAL`.
4. `NEED_APPROVAL` before `SANITIZE`: approval must occur before redacted content can run.
5. `SANITIZE`, then `ALLOW`.

Risk score and level take the **maximum** of the two sides.

The upstream engine's own verdict is not only policy packs. Inside the engine:

- The final verdict is the stronger of the policy-pack decision and a **risk-band decision**: a `CRITICAL` band
  (score >= 0.85) becomes `BLOCK`, `HIGH` (>= 0.6) becomes `NEED_APPROVAL`. Accumulated lower-severity findings cap
  at 0.84, so only critical entities or a `CRITICAL` policy floor reach `BLOCK`.
- **Low-confidence override:** a `NEED_APPROVAL` becomes `ASK_USER` when planner `confidence < 0.75` for
  `API_CALL`, `BROWSER_SUBMIT`, `BROWSER_CLICK`, `BROWSER_TYPE`, `FILE_WRITE` or `FILE_DELETE`, unless a detector
  failed. `BLOCK` and `SANITIZE` are not softened.
- If the application rules already return `BLOCK`, the detectors are not run at all.

## Host-enforced rules

These hold regardless of what the planner claims or the detectors return:

| Rule | Effect |
|------|--------|
| **Registered operations only** | Only known `(target_system, operation)` pairs run (`local_file.read`, `github.repo_metadata`, `gmail.list_messages`, `calendar.list_events/create_event`, `telegram.*`, `stripe.*`). Anything else is `BLOCK` with policy `host.registered_tool_required` |
| **Trusted risk hints per operation** | e.g. calendar create and Telegram sends carry `external_send`; Stripe checkout `payment`; refunds `refund`; expiring a session `payment` + `destructive_action`. The planner's own `risk_hint` can only raise the floor, not lower it |
| **Secrets never leave** | If a credential entity appears in an `API_CALL`, `BROWSER_SUBMIT`, or `BROWSER_TYPE` action: `BLOCK` (`code.secret_egress`) |
| **Unredactable secrets** | A secret that cannot be rewritten into a supported content field is `BLOCK` (`host.unredactable_sensitive_content`) |
| **SANITIZE without a replacement** | Downgraded to `NEED_APPROVAL` |
| **Detector failure** | Unavailable or malformed detector response means approval is required; failing to persist the evaluation prevents execution |
| **Payment in browser flows** | Payment-related browser actions add the `payment` hint |

Detector prompts receive a copy with known credentials already masked, so secrets are not sent to Ollama.

## Application rule priority (deterministic rules)

Applied in order; first match wins (`app/domains/guardrail/decision/simple.py`):

| # | Condition | Decision | Risk |
|---|-----------|----------|------|
| 1 | `risk_hint` in `GUARDRAIL_BLOCK_HINTS` | `BLOCK` | CRITICAL, 0.95 |
| 2 | Secret-shaped content in payload | `SANITIZE` (redacted preview) | MEDIUM, 0.50 |
| 3 | `risk_hint` in `GUARDRAIL_ASK_USER_HINTS` | `ASK_USER` | LOW, 0.30 |
| 4 | `risk_hint` in `GUARDRAIL_NEED_APPROVAL_HINTS`, or domain risk is CRITICAL/HIGH | `NEED_APPROVAL` | domain risk (CRITICAL for `payment`/`refund`), 0.60 / 0.80 |
| 5 | Otherwise | `ALLOW` | LOW, 0.10 |

Domain base risk: `booking` CRITICAL, `code_protection` HIGH, `productivity` MEDIUM, `browser` LOW,
`filesystem` LOW. Because `booking` (Stripe) and `code_protection` (GitHub) are high risk, every action in those
domains needs approval, including read-only ones such as `github.repo_metadata` and `stripe.retrieve_*`. Nothing in
the merge can lower this floor.

Secret patterns redacted: `sk-...` keys, Stripe `sk_/rk_` keys and `whsec_` secrets, AWS `AKIA...`, GitHub
`gh?_...` tokens, and `password|passwd|pwd|secret|api_key|access_token|auth_token = value` assignments (a bare `token = value` is not matched).

## Policy packs

JSON rule packs in `app/domains/guardrail/_vendor/agentgate/policy/packs/`:

| Pack | Rules (decision) |
|------|------------------|
| `global_safety` | Prompt injection (BLOCK); credential-request phishing (BLOCK); bulk PII egress (BLOCK); destructive action (NEED_APPROVAL, higher if no rollback); low planner confidence `<= 0.5` on `API_CALL`/`BROWSER_SUBMIT`/`BROWSER_CLICK` (NEED_APPROVAL in the pack, but the engine's low-confidence override turns it into `ASK_USER`); PII in external action (SANITIZE); browser submit and submit-via-click (NEED_APPROVAL) |
| `code_data` | Secret egress (BLOCK); secret present (SANITIZE); env/credentials file access (NEED_APPROVAL); source code + external send (NEED_APPROVAL); local file write (NEED_APPROVAL); local file delete (NEED_APPROVAL) |
| `productivity` | Bulk action (NEED_APPROVAL, higher if irreversible); external email send (NEED_APPROVAL); calendar create/update (ALLOW) |
| `booking` | External payment message (NEED_APPROVAL); browser submit in booking flow (NEED_APPROVAL); customer PII to external (SANITIZE); cancelling a booking (NEED_APPROVAL) |

A rule can set a decision and a `risk_floor`. Rules match on domains, action types, target systems, risk hints,
entity kinds, tags, `requires_no_rollback`, and `min_confidence`.

## Detectors

Six detectors run on the local Ollama model, dispatched concurrently: **PII**, **secrets**, **source code**,
**payment phishing**, **prompt injection**, **action intent**. `AGENTGATE_DETECTOR_ARCHITECTURE=unified`
switches to one experimental combined detector.

## Sensitive input ("sanitize" pause)

Separate from redaction. `app/domains/guardrail/sensitive.py` detects when a plan step **needs the user to type
something** and pauses the run (`waiting_input`) until `/respond` with `action=input`:

- a value that is a placeholder: `{{name}}`, `{name}`, `<name>`, `[name]`
- a fabricated fill-in: `YOUR_PASSWORD_HERE`, or a bare `password`, `token`, `email`, `otp`, ...
- a secret-named key (`password`, `token`, `api_key`, `otp`, `pin`, `cvv`, `card_number`, ...) with an empty value
- an empty `value` on `BROWSER_TYPE`/`BROWSER_SELECT`

Keys `label`, `role`, `element_id` are descriptive and never treated as fill-ins. Fields already answered are not
asked again.

## Human interaction

| Situation | What happens |
|-----------|--------------|
| `NEED_APPROVAL` | Run pauses; approve executes (with the redacted payload if one exists), decline stops the step |
| `SANITIZE` | User reviews/confirms the redacted preview or supplies missing input |
| `ASK_USER` | Clarification is requested; it is **not** approval of a calendar or financial write |
| CLI, non-interactive | Required approval or input fails closed with exit code 3; there is no `--yes` bypass |

## Tuning

- Change which hints map to which verdict with `GUARDRAIL_BLOCK_HINTS`, `GUARDRAIL_NEED_APPROVAL_HINTS`,
  `GUARDRAIL_ASK_USER_HINTS` (values must come from `ALLOWED_RISK_HINTS`, plus the ASK_USER set).
- Add or edit policy pack rules in the pack JSON files. Bump `policy_version` in audit records when you do
  (currently the constant `policy-0.1`).
- Prefer stricter over looser: the host rules mean loosening a hint cannot unblock an unregistered operation.
