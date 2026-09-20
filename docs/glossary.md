# Glossary

| Term | Meaning |
|------|---------|
| **Action** | One operation the planner proposes (read a file, send a message, click a button) |
| **ActionRequest** | The validated description of an action, before evaluation |
| **Agent loop** | Plan, guardrail, execute, observe, replan cycle for a run |
| **Approval** | A human decision to run (or decline) an action the guardrail flagged as `NEED_APPROVAL` |
| **Audit event / row** | The immutable record of an action's request, decision, and outcome |
| **BLOCK** | Verdict: never execute |
| **Connector** | Adapter that performs an operation on an external system (`BaseConnector`) |
| **Decision router** | `ExecutionRouter`: sends allowed actions to the API or browser executor |
| **Detector** | An LLM-backed check (PII, secrets, source code, payment phishing, prompt injection, action intent) |
| **Domain** | Risk category of an action: `browser`, `productivity`, `code_protection`, `booking`, `filesystem` |
| **Executor** | Component that runs an action: `APIExecutor` or `BrowserExecutor` |
| **Fail closed** | On error or uncertainty, require approval or block instead of allowing |
| **Guardrail** | The evaluation layer that produces a `DecisionResponse` |
| **Guardrail journal** | `guardrail.jsonl`: one entry per guardrail evaluation |
| **Host rules** | Application-enforced rules (registered operations, trusted hints, secret egress) that detectors cannot override |
| **Policy pack** | JSON file of rules mapping conditions to a decision and a risk floor |
| **Redaction / sanitize** | Masking sensitive content so an action can proceed with a safe payload |
| **Rollback available** | Whether an action can be undone; irreversible actions raise risk |
| **Risk hint** | A label (`external_send`, `payment`, `destructive`, ...) describing an action's impact |
| **Run** | One user request handled by the agent loop, identified by `run_id` |
| **Sanitize pause** | A run waiting for the user to type a missing or sensitive value (`waiting_input`) |
| **Selector map** | Server-side map from short element IDs to real browser locators; never shown to the planner |
| **Step** | One planned action within a run |
| **Trace** | `ActionTrace`: model-ready export of the whole action chain |
