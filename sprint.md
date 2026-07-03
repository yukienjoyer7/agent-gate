# AgentGate Data Engineer Sprint Plan

**Owner:** Data Engineer (DE)  
**Project:** AgentGate — Framework-Agnostic Core Guardrail Engine  
**Target launch:** 2026-10-02  
**Primary DE responsibility:** Build the execution, audit, connector, browser automation, logging, trace, and reproducibility infrastructure required for the AgentGate MVP.

---

## 0. PRD Baseline

The PRD defines AgentGate as a Python guardrail runtime that evaluates proposed AI-agent actions before they reach APIs, browser automation, local files, or external systems. The MVP path is:

```text
User / Web Chat / CLI
→ DS-led custom function-calling loop
→ ActionRequest
→ AgentGate Core evaluation
→ Decision Router
→ API Executor or Browser Executor
→ ExecutionResult
→ Audit Log / Dashboard / Benchmark
```

The DE workstream must support:

- API execution for Gmail, Google Calendar, GitHub, Stripe Sandbox, Telegram, and local filesystem.
- Browser execution through Playwright: open, snapshot, click, type, select, submit, screenshot.
- Simplified browser snapshots with `element_id`, `label`, `role`, `text`, `value_preview`, `risk_hint`, and internal `selector_map`.
- Audit completeness for request, decision, reasons, status, timestamp, and latency.
- Raw-vs-guarded latency benchmarking.
- Reproducible demo scripts, connector docs, deployment notes, and final artifact manifest.

---

## 1. Engineering Constraints

### 1.1 MVP boundaries

Do not implement these as core MVP deliverables:

- Chrome/browser extension.
- MCP server runtime.
- LangGraph workflow runtime.
- OpenClaw production plugin.
- Production access to real private dashboards.
- Full enterprise DLP, SIEM/SOC, IAM/RBAC, or compliance system.
- Fully autonomous external-send, destructive, financial, delete/cancel/refund, or bulk actions without approval.

### 1.2 Connector quality tiers

| Tier | Connector / subsystem | Launch expectation |
|---|---|---|
| Tier 1 | Local filesystem | Stable demo-critical path |
| Tier 1 | GitHub | Stable demo-critical path |
| Tier 1 | Gmail | Stable demo-critical path or realistic sandbox path |
| Tier 1 | Playwright Browser Executor | Stable demo-critical path |
| Tier 2 | Google Calendar | Basic working path or documented stub |
| Tier 2 | Telegram | Basic working path or documented stub |
| Tier 3 | Stripe Sandbox | Sandbox/mock only unless time allows |

### 1.3 Non-negotiable DE principles

- Every proposed action must become an `ActionRequest` before execution.
- Every evaluated action must produce a `DecisionResponse`.
- Every execution attempt must produce an `ExecutionResult`.
- Every action must be written to the audit store, including blocked and failed actions.
- Browser `selector_map` must stay server-side. The LLM sees only short element IDs.
- Browser actions must revalidate snapshot-scoped selectors before click/type/select/submit.
- Raw tokens, API keys, OAuth refresh tokens, full credentials, and private file contents must never be logged.
- All public contracts must include `schema_version`.
- All logs/traces must include `run_id` and `action_id`.

---

## 2. Target Repository Structure

Codex should converge toward this structure unless the actual repo already has a better equivalent.

```text
agentgate/
  backend/
    app/
      main.py
      api/
        routes_runs.py
        routes_actions.py
        routes_audit.py
        routes_approvals.py
        routes_benchmarks.py
      core/
        schemas.py
        action_request.py
        decision_response.py
        execution_result.py
        errors.py
      executors/
        api_executor.py
        browser_executor.py
        router.py
      connectors/
        base.py
        local_file.py
        github.py
        gmail.py
        calendar.py
        telegram.py
        stripe_sandbox.py
      browser/
        playwright_session.py
        snapshot_builder.py
        selector_map.py
        revalidation.py
      audit/
        db.py
        models.py
        repository.py
        migrations/
      tracing/
        logger.py
        trace_writer.py
        latency.py
      config/
        settings.py
      tests/
        unit/
        integration/
        fixtures/
    scripts/
      seed_demo.py
      run_demo_scenario.py
      run_benchmark.py
      export_traces.py
      export_audit.py
  docs/
    adr/
    connectors/
    browser-tool-contract.md
    api-executor-contract.md
    audit-schema.md
    reproducibility.md
  artifacts/
    runs/
  .env.example
  docker-compose.yml
  README.md
  sprint.md
```

---

## 3. Canonical Runtime Contracts

These contracts should be implemented as Pydantic models or JSON Schema early, then treated as semi-frozen.

### 3.1 ActionRequest

```json
{
  "schema_version": "0.1",
  "run_id": "run_001",
  "action_id": "act_001",
  "source": "custom_loop|web|cli|replay",
  "domain": "productivity|booking|code_protection|browser|filesystem",
  "action_type": "API_CALL|BROWSER_OPEN|BROWSER_SNAPSHOT|BROWSER_CLICK|BROWSER_TYPE|BROWSER_SELECT|BROWSER_SUBMIT|BROWSER_SCREENSHOT|FILE_READ",
  "target_system": "gmail|github|calendar|telegram|stripe|local_file|browser",
  "target": "string or object summary",
  "content_context": "short redacted context",
  "payload_summary": "short redacted payload summary",
  "browser_element": {
    "snapshot_id": "snap_001",
    "element_id": "e_002",
    "role": "button",
    "label": "Send Message",
    "text": "Send Message",
    "risk_hint": "external_send"
  },
  "risk_hint": "external_send|bulk_action|file_read|source_code|payment|destructive|unknown",
  "rollback_available": false,
  "confidence": 0.74,
  "created_at": "ISO-8601 timestamp"
}
```

### 3.2 DecisionResponse

```json
{
  "schema_version": "0.1",
  "run_id": "run_001",
  "action_id": "act_001",
  "decision": "ALLOW|BLOCK|NEED_APPROVAL|SANITIZE|ASK_USER",
  "risk_level": "LOW|MEDIUM|HIGH|CRITICAL",
  "risk_score": 0.82,
  "reasons": ["external customer target", "browser submit/send action"],
  "triggered_policies": ["external_send_requires_approval"],
  "sensitive_entities": [],
  "sanitized_payload": null,
  "next_step": "approval_queue",
  "latency_ms": 18,
  "created_at": "ISO-8601 timestamp"
}
```

### 3.3 ExecutionResult

```json
{
  "schema_version": "0.1",
  "run_id": "run_001",
  "action_id": "act_001",
  "executor": "api|browser|local_file|mock",
  "status": "SUCCESS|FAILED|SKIPPED|BLOCKED|PENDING_APPROVAL",
  "result_summary": "short result summary",
  "error": null,
  "latency_ms": 130,
  "created_at": "ISO-8601 timestamp"
}
```

### 3.4 AuditEvent

```json
{
  "schema_version": "0.1",
  "audit_id": "aud_001",
  "run_id": "run_001",
  "action_id": "act_001",
  "request_json": {},
  "decision_json": {},
  "execution_json": {},
  "execution_status": "SUCCESS|FAILED|SKIPPED|BLOCKED|PENDING_APPROVAL",
  "error_type": null,
  "policy_version": "policy-0.1",
  "detector_version": "detector-0.1",
  "latency": {
    "action_request_ms": 5,
    "guardrail_ms": 18,
    "audit_write_ms": 7,
    "executor_ms": 130,
    "total_ms": 160
  },
  "created_at": "ISO-8601 timestamp"
}
```

---

## 4. Global Definition of Done

A sprint is done only when:

- Code is merged or ready for merge.
- Tests pass for changed modules.
- Schema changes are documented.
- `.env.example` is updated if config changed.
- Audit logging captures new action paths.
- Errors are normalized through `ConnectorError` or equivalent.
- Reproducible command exists for any new demo/test path.
- No raw secret, token, or private payload is logged.

---

## 4.1 Current Repo Audit — Sprint 0/1 Alignment

**Audit date:** 2026-07-03  
**Basis:** current `main` branch plus local `sprint.md`.

### What is actually done

- FastAPI app factory with `/` and `/api/v1/health`.
- Python package scaffold under `app/` with API, domain, executor, LLM, database, worker, config, and utility package boundaries.
- `pyproject.toml` with FastAPI, SQLAlchemy, Alembic, Pydantic settings, Playwright, Redis, HTTPX, and dev tooling.
- Environment loading through `app/config/settings.py`.
- Structured logging setup.
- `.env.example` with core, database, queue, LLM, and connector placeholders.
- PostgreSQL/Redis `docker-compose.yml`.
- Dockerfile under `deployment/docker/`.
- SQLAlchemy declarative base, async session factory, and Alembic scaffold.
- `BaseConnector.execute(action, payload)` interface.
- Placeholder API router files for audits, approvals, benchmark, chat, and scenarios.
- Architecture foundation ADR at `docs/decisions/0001-architecture-foundation.md`.
- README quickstart, layout, contributing, and local test instructions.
- Health/root tests in `tests/test_health.py`.

### What `sprint.md` previously listed but is not present

- Separate Phase 0 ADR placeholders for database, auth storage, selector map, and audit logging.
- Phase 1 ADR set for Playwright, browser snapshots, selector map, API auth, audit DB, and connector errors.
- Runtime schemas for `ActionRequest`, `DecisionResponse`, `ExecutionResult`, `AuditEvent`, approvals, browser snapshots, or connector errors.
- ActionRequest builder, execution router, API/browser executors, local file/GitHub/Gmail connectors, audit repository/models, demo scripts, export scripts, trace writer, or benchmark runner.
- Browser snapshot, selector map, revalidation, or Playwright session implementation.

**Alignment decision:** Sprint 0 is treated as completed as a repository foundation sprint. The planned research/ADR and execution-pipeline work remains future work unless implemented in later commits.

---

# 5. Sprint Backlog

## Phase 0 — Project Foundation

**Dates:** 2026-06-19 to 2026-06-21  
**Status:** Done in current repo, with actual outputs listed below.  
**Sprint goal:** Establish project structure, local environment, database direction, connector credential plan, and environment strategy.

### Deliverables

- Initial repository structure.
- Local dev setup.
- Architecture/database/auth direction in `docs/decisions/0001-architecture-foundation.md`.
- `.env.example`.
- Minimal README setup instructions.
- FastAPI health/root endpoint and tests.
- Docker Compose for PostgreSQL and Redis.
- Alembic/SQLAlchemy scaffold.

### Codex task prompt

```text
You are working on the AgentGate backend as the Data Engineer.
Create the initial project foundation for a Python MVP.
Set up a clean backend structure with modules for API routes, domains, executors, connectors, browser automation, audit logging, config, database, docs, and tests.
Add .env.example, README local setup instructions, Docker Compose, Alembic/SQLAlchemy scaffolding, and an architecture foundation ADR.
Do not implement product logic yet. Focus on structure and reproducibility.
```

### Backlog

| ID | Status | Priority | Task | Output | Acceptance criteria |
|---|---|---:|---|---|---|
| DE-P0-01 | Done | P0 | Create backend repo/module structure | `app/` packages + `__init__.py` files | Imports work; structure approximates target layout |
| DE-P0-02 | Done | P0 | Add config loader | `app/config/settings.py` | Loads `.env`; defaults are safe for local dev |
| DE-P0-03 | Done | P0 | Add `.env.example` | env template | No real secrets; includes DB, queue, LLM, connector, logging vars |
| DE-P0-04 | Done | P0 | Decide architecture/database/auth direction | `docs/decisions/0001-architecture-foundation.md` | Documents PostgreSQL, SQLAlchemy, Alembic, `.env`, future vault path |
| DE-P0-05 | Done | P1 | Add Docker Compose skeleton | `docker-compose.yml` | Postgres and Redis services documented |
| DE-P0-06 | Done | P1 | Add README local setup | `README.md` | New dev can install, configure env, and run placeholder app |
| DE-P0-07 | Done | P1 | Add health check test | `tests/test_health.py` | Health and root endpoints pass |

### Exit criteria

```text
uvicorn app.main:app --reload
```

or equivalent should run without crashing.

---

## Phase 1 — Technical Research and ADRs

**Dates:** 2026-06-22 to 2026-06-28  
**Status:** Not completed in current repo; only the architecture foundation ADR exists.  
**Sprint goal:** Convert research topics into engineering decisions.

### Deliverables

- Architecture foundation ADR completed.
- Playwright architecture ADR remains pending.
- Browser snapshot ADR remains pending.
- Selector map ADR remains pending.
- API auth ADR remains pending.
- Audit DB ADR remains pending.
- Connector error taxonomy remains pending.

### Codex task prompt

```text
Create technical ADR documents for AgentGate DE infrastructure.
Cover Playwright browser execution, browser snapshot extraction, selector_map safety, API auth handling, audit DB design, and connector error handling.
Keep each ADR practical: context, decision, alternatives considered, consequences, and MVP implementation notes.
Also create a connector error taxonomy document with normalized error codes.
```

### Backlog

| ID | Status | Priority | Task | Output | Acceptance criteria |
|---|---|---:|---|---|---|
| DE-P1-00 | Done | P0 | Write architecture foundation ADR | `docs/decisions/0001-architecture-foundation.md` | Documents major stack and module-boundary decisions |
| DE-P1-01 | Pending | P0 | Write Playwright execution ADR | `docs/decisions/0002-playwright-executor.md` | Defines session model, timeouts, screenshot policy |
| DE-P1-02 | Pending | P0 | Write snapshot ADR | `docs/decisions/0003-browser-snapshot.md` | Defines fields extracted from DOM and redaction behavior |
| DE-P1-03 | Pending | P0 | Write selector map ADR | `docs/decisions/0004-selector-map.md` | Snapshot-scoped IDs; server-side map; revalidation rules |
| DE-P1-04 | Pending | P0 | Write API auth ADR | `docs/decisions/0005-api-auth.md` | Defines env/OAuth handling and no-secret-logging rules |
| DE-P1-05 | Pending | P0 | Write audit DB ADR | `docs/decisions/0006-audit-db.md` | Defines append-only log, IDs, indexes, JSON storage |
| DE-P1-06 | Pending | P1 | Define connector errors | `docs/connectors/error-taxonomy.md` | Auth, permission, rate limit, timeout, validation, unavailable, unknown |

### Exit criteria

Architecture foundation is written; high-risk infrastructure ADRs remain open.

---

## Phase 2 — Schema and Contract Design

**Dates:** 2026-06-29 to 2026-07-05  
**Sprint goal:** Freeze v0.1 runtime contracts for DS, FE, DA, and DE integration.

### Deliverables

- Pydantic schemas or JSON schemas.
- Contract documentation.
- Deployment boundary note.
- Unit tests for schema validation.

### Codex task prompt

```text
Implement AgentGate v0.1 runtime schemas as Pydantic models.
Create models for ActionRequest, DecisionResponse, APIExecutorRequest, BrowserExecutorRequest, BrowserSnapshot, BrowserElement, ExecutionResult, AuditEvent, ApprovalRecord, and ConnectorError.
Every public schema must include schema_version, run_id/action_id where applicable, timestamps, and safe serialization.
Add unit tests for valid and invalid examples.
Generate or write Markdown docs explaining the contracts.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-P2-01 | P0 | Implement `ActionRequest` | `core/schemas.py` | Validates core fields and supported action types |
| DE-P2-02 | P0 | Implement `DecisionResponse` | `core/schemas.py` | Supports ALLOW, BLOCK, NEED_APPROVAL, SANITIZE, ASK_USER |
| DE-P2-03 | P0 | Implement executor requests | `core/schemas.py` | API and browser requests validate required fields |
| DE-P2-04 | P0 | Implement browser snapshot schemas | `core/schemas.py` | Includes snapshot_id, elements, metadata, redaction flags |
| DE-P2-05 | P0 | Implement `ExecutionResult` | `core/schemas.py` | Supports SUCCESS, FAILED, SKIPPED, BLOCKED, PENDING_APPROVAL |
| DE-P2-06 | P0 | Implement `AuditEvent` | `core/schemas.py` | Stores request, decision, execution, latency, versions |
| DE-P2-07 | P1 | Implement `ApprovalRecord` | `core/schemas.py` | Tracks pending approval and reviewer decision |
| DE-P2-08 | P1 | Implement `ConnectorError` | `core/errors.py` | Normalized error fields and error codes |
| DE-P2-09 | P0 | Add schema tests | `tests/unit/test_schemas.py` | Valid/invalid fixtures pass as expected |
| DE-P2-10 | P1 | Document contracts | `docs/runtime-contracts.md` | DS/FE can use docs without reading source |
| DE-P2-11 | P1 | Define deployment boundaries | `docs/deployment-boundaries.md` | Clarifies core vs executor vs UI responsibilities |

### Exit criteria

Schemas can be imported and validated independently from the rest of the app.

---

## Phase 3 — Browser and Audit Prototype

**Dates:** 2026-07-06 to 2026-07-12  
**Sprint goal:** Build the first vertical slice from browser snapshot to audit write.

### Deliverables

- Playwright `browser_open` prototype.
- Playwright `browser_snapshot` prototype.
- Internal selector map.
- Selector revalidation prototype.
- API connector stubs.
- Audit write path.
- One end-to-end prototype.

### Codex task prompt

```text
Build the first AgentGate DE vertical slice.
Implement a Playwright browser session that can open a page and produce a simplified BrowserSnapshot.
Extract URL, title, visible text summary, interactive elements, roles, labels, text, value previews, and nearby context.
Generate snapshot-scoped element IDs and keep a server-side selector_map.
Implement selector revalidation before browser actions.
Add API connector stubs and an audit write path that records ActionRequest, mock DecisionResponse, and ExecutionResult.
Create one demo script that runs open → snapshot → mock decision → audit write.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-P3-01 | P0 | Implement Playwright session wrapper | `browser/playwright_session.py` | Can open and close browser reliably |
| DE-P3-02 | P0 | Implement snapshot builder | `browser/snapshot_builder.py` | Extracts visible interactive elements |
| DE-P3-03 | P0 | Implement selector map | `browser/selector_map.py` | Maps `snapshot_id + element_id` to selector internally |
| DE-P3-04 | P0 | Implement revalidation prototype | `browser/revalidation.py` | Checks URL, visibility, role, label/text before action |
| DE-P3-05 | P1 | Add API connector base/stubs | `connectors/base.py` + stubs | Stubs return normalized `ExecutionResult` |
| DE-P3-06 | P0 | Implement audit write path | `audit/repository.py` | Writes action, decision, execution, timestamp |
| DE-P3-07 | P0 | Add demo script | `scripts/run_demo_scenario.py` | Runs one browser snapshot demo end-to-end |
| DE-P3-08 | P1 | Add mock reservation page fixture | `tests/fixtures/mock_pages/` | Browser snapshot has expected elements |

### Exit criteria

A local command can create a browser snapshot and write a complete audit record.

---

## Break Buffer

**Dates:** 2026-07-13 to 2026-07-19  
**Sprint goal:** Stabilize foundation. No planned feature expansion.

### Codex task prompt

```text
Stabilize the AgentGate DE foundation.
Review schemas, docs, local setup, and prototype scripts.
Fix import issues, flaky setup, missing tests, missing env documentation, and incomplete README instructions.
Do not add new major features.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-B0-01 | P0 | Clean imports and module layout | code cleanup | `pytest` imports modules cleanly |
| DE-B0-02 | P0 | Improve README | `README.md` | Setup and demo commands are accurate |
| DE-B0-03 | P1 | Add schema fixtures | `tests/fixtures/` | Reusable valid/invalid request examples exist |
| DE-B0-04 | P1 | Update env docs | `.env.example` + README | All config vars documented |

### Exit criteria

Main branch is stable enough for Sprint 1 implementation.

---

## Sprint 1 — Core Execution Infrastructure

**Dates:** 2026-07-20 to 2026-08-02  
**Status:** Implemented as a minimal runnable vertical slice on 2026-07-03.  
**Sprint goal:** Implement the initial ActionRequest pipeline, audit DB, connector baseline, and Playwright executor skeleton.

### Deliverables

- ActionRequest pipeline.
- Append-only JSONL audit prototype.
- Local file connector.
- GitHub connector mock baseline.
- Gmail connector mock baseline.
- Browser executor skeleton.
- End-to-end guarded execution flow.

### Codex task prompt

```text
Implement Sprint 1 Data Engineer infrastructure for AgentGate.
Build the ActionRequest pipeline, audit database prototype, append-only audit logging, local filesystem connector, GitHub connector baseline, Gmail connector baseline, and Playwright executor skeleton.
All action paths must produce ActionRequest → DecisionResponse → ExecutionResult → AuditEvent.
Use normalized ConnectorError for failures.
Do not log raw secrets, tokens, or private payloads.
Add tests and at least one CLI/demo path for a guarded local file action and one guarded browser action.
```

### Backlog

| ID | Status | Priority | Task | Output | Acceptance criteria |
|---|---|---:|---|---|---|
| DE-S1-01 | Done | P0 | Implement ActionRequest builder | `app/core/action_request.py` | Converts raw tool proposal into validated `ActionRequest` |
| DE-S1-02 | Deferred | P0 | Implement DB connection + migrations | existing Alembic scaffold | Full DB audit schema deferred to Sprint 2/4; JSONL is the Sprint 1 runnable audit store |
| DE-S1-03 | Done | P0 | Implement append-only audit repository | `app/domains/audit/repositories/audit_repository.py` | Every demo action path writes complete audit event |
| DE-S1-04 | Done | P0 | Implement local file connector | `app/domains/connector/filesystem/local_file.py` | Supports safe read of allowed demo directory only |
| DE-S1-05 | Done | P1 | Implement GitHub baseline connector | `app/domains/connector/github/github.py` | Mock mode returns normalized `ExecutionResult` |
| DE-S1-06 | Done | P1 | Implement Gmail baseline connector | `app/domains/connector/gmail/gmail.py` | Mock mode returns normalized `ExecutionResult` |
| DE-S1-07 | Done | P0 | Implement browser executor skeleton | `app/executors/browser_executor.py` | Supports open/snapshot skeletons and skips unsupported actions cleanly |
| DE-S1-08 | Done | P0 | Implement API executor skeleton | `app/executors/api_executor.py` | Routes connector calls by target system |
| DE-S1-09 | Done | P0 | Implement execution router | `app/executors/router.py` | Routes API/browser/local action after decision |
| DE-S1-10 | Done | P0 | Add guarded local file demo | `scripts/run_demo_scenario.py local_file_read` | Full audit trace generated |
| DE-S1-11 | Done | P0 | Add guarded browser demo | `scripts/run_demo_scenario.py browser_snapshot` | Mock browser snapshot audit trace generated |
| DE-S1-12 | Done | P1 | Add basic integration tests | `tests/integration/test_sprint1_flow.py` | End-to-end demo paths pass |

### Exit criteria

These commands or equivalents work:

```bash
python scripts/run_demo_scenario.py local_file_read
python scripts/run_demo_scenario.py browser_snapshot
python scripts/export_audit.py --latest
```

---

## Sprint 1B — API and Browser Pilot

**Dates:** 2026-08-03 to 2026-08-09  
**Sprint goal:** Validate the pilot reliability of API execution, browser snapshotting, selector mapping, connector auth, and audit storage.

### Deliverables

- Pilot scenarios for Booking, Code Protection, and Productivity.
- Snapshot quality metrics.
- Selector reliability metrics.
- Connector auth validation.
- Audit completeness report.
- Pilot failure report.

### Codex task prompt

```text
Run and improve the AgentGate API/browser pilot.
Add pilot scenarios for booking, code protection, and productivity use cases.
Measure browser snapshot completeness, selector_map reliability, connector auth outcomes, and audit storage completeness.
Add selector revalidation checks for URL, role, label/text, visibility, and stale snapshot handling.
Create a pilot report generator that summarizes failures and reliability metrics.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-S1B-01 | P0 | Add scenario fixtures | `tests/fixtures/scenarios/` | Booking, code protection, productivity scenarios exist |
| DE-S1B-02 | P0 | Add snapshot completeness metric | `tracing/metrics.py` | Reports detected/expected interactive elements |
| DE-S1B-03 | P0 | Add selector revalidation checks | `browser/revalidation.py` | URL, role, text/label, visibility checked before action |
| DE-S1B-04 | P0 | Add stale snapshot error | `core/errors.py` | Stale snapshot produces normalized error |
| DE-S1B-05 | P1 | Validate connector auth modes | connector docs/tests | Missing/invalid credentials fail safely |
| DE-S1B-06 | P0 | Add audit completeness check | `scripts/check_audit_completeness.py` | Verifies request, decision, execution, status, timestamp |
| DE-S1B-07 | P1 | Generate pilot report | `scripts/generate_pilot_report.py` | Outputs Markdown or JSON summary |

### Exit criteria

At least one API/local scenario and one browser scenario complete end-to-end with complete audit logs.

---

## Sprint 2 — Executor Pipelines and Action Traces

**Dates:** 2026-08-10 to 2026-08-23  
**Sprint goal:** Build stable API/browser executor pipelines, service contracts, versioned logs, and model-ready action traces.

### Deliverables

- API executor pipeline.
- Browser executor pipeline.
- Service contracts for DS/FE integration.
- Versioned logs.
- JSONL trace export.
- Run/action endpoints.
- Latency instrumentation.

### Codex task prompt

```text
Implement Sprint 2 executor pipelines and tracing for AgentGate.
Create stable API and browser executor pipelines with service contracts for the DS loop and FE dashboard.
Propagate run_id and action_id across every layer.
Add latency tracking for ActionRequest building, guardrail evaluation, audit write, API execution, browser execution, and total action time.
Implement JSONL trace export and backend endpoints for runs, actions, audit events, approval queue data, and benchmark results.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-S2-01 | P0 | Harden API executor pipeline | `executors/api_executor.py` | Validated request in, normalized result out |
| DE-S2-02 | P0 | Harden browser executor pipeline | `executors/browser_executor.py` | open/snapshot/click/type/select/submit/screenshot contracts exist |
| DE-S2-03 | P0 | Add run/action ID propagation | core/executor/audit | All logs and traces include IDs |
| DE-S2-04 | P0 | Add latency tracker | `tracing/latency.py` | Captures per-stage timings |
| DE-S2-05 | P0 | Add trace writer | `tracing/trace_writer.py` | Writes JSONL traces per run |
| DE-S2-06 | P0 | Add run history endpoint | `api/routes_runs.py` | FE can list runs |
| DE-S2-07 | P0 | Add action detail endpoint | `api/routes_actions.py` | FE can inspect action request/decision/result |
| DE-S2-08 | P0 | Add audit endpoint | `api/routes_audit.py` | FE/DA can query audit events |
| DE-S2-09 | P1 | Add approval queue data endpoint | `api/routes_approvals.py` | Returns pending approval records |
| DE-S2-10 | P1 | Add benchmark endpoint | `api/routes_benchmarks.py` | Returns latest benchmark summaries |
| DE-S2-11 | P0 | Add trace export script | `scripts/export_traces.py` | Exports JSONL for DS/DA evaluation |
| DE-S2-12 | P1 | Document service contracts | `docs/service-contracts.md` | FE/DS can integrate without guessing fields |

### Exit criteria

A full run produces a model-ready trace with user goal, raw tool call, ActionRequest, DecisionResponse, ExecutionResult, audit metadata, latency breakdown, and final status.

---

## Sprint 3 — Connector and Browser Stabilization

**Dates:** 2026-08-24 to 2026-09-06  
**Sprint goal:** Stabilize demo-critical connectors, Playwright actions, selector mapping, and error recovery.

### Deliverables

- Stable Tier 1 connectors.
- Documented Tier 2/Tier 3 connector status.
- Playwright action recovery.
- Structured errors and retry policy.
- Idempotency protection for risky actions.

### Codex task prompt

```text
Stabilize AgentGate connectors and browser executor.
Prioritize Tier 1 systems: local filesystem, GitHub, Gmail, and Playwright.
Add robust error handling for expired credentials, permission denied, rate limits, malformed payloads, timeouts, service unavailable, element not found, stale selector, unexpected navigation, and modal blocking action.
Add safe retry behavior only for idempotent or explicitly safe actions.
Do not expand optional connectors if Tier 1 is unstable.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-S3-01 | P0 | Stabilize local file connector | `connectors/local_file.py` | Path allowlist enforced; no arbitrary read |
| DE-S3-02 | P0 | Stabilize GitHub connector | `connectors/github.py` | Safe read/list actions work or mock reliably |
| DE-S3-03 | P0 | Stabilize Gmail connector | `connectors/gmail.py` | Safe search/list/archive mock or sandbox path works |
| DE-S3-04 | P1 | Stabilize Calendar connector | `connectors/calendar.py` | Basic path or documented stub |
| DE-S3-05 | P1 | Stabilize Telegram connector | `connectors/telegram.py` | Basic path or documented stub |
| DE-S3-06 | P2 | Stabilize Stripe Sandbox connector | `connectors/stripe_sandbox.py` | Sandbox/mock path only |
| DE-S3-07 | P0 | Harden browser click/type/select/submit | `executors/browser_executor.py` | Common browser actions return clear result/error |
| DE-S3-08 | P0 | Add browser recovery cases | `browser/revalidation.py` | Handles element missing, stale selector, timeout, navigation change |
| DE-S3-09 | P1 | Add modal/interstitial detection | browser module | Blocks or asks user when modal changes target context |
| DE-S3-10 | P1 | Add idempotency keys | executor/audit | Risky repeated requests are detectable |
| DE-S3-11 | P0 | Normalize connector errors | connectors + errors | All failures map to standard error taxonomy |
| DE-S3-12 | P1 | Add safe retry policy | executors | Retries only safe retryable failures |

### Exit criteria

Demo-critical flows run repeatedly without silent failures, and every failed action is visible in audit with actionable error details.

---

## Sprint 4 — Infrastructure Freeze and Documentation

**Dates:** 2026-09-07 to 2026-09-13  
**Sprint goal:** Finalize infrastructure contracts, database schema, docs, packaging, env config, and reproducibility commands.

### Deliverables

- Frozen DB schema.
- Final connector docs.
- Browser tool contract.
- API executor contract.
- Reproducibility scripts.
- CLI packaging.
- Seed data script.
- Benchmark commands.
- Deployment notes.

### Codex task prompt

```text
Finalize AgentGate DE infrastructure for release preparation.
Freeze the database schema, connector config format, API executor contract, browser tool contract, .env loading behavior, and reproducibility scripts.
Write clear docs for connector setup, permissions/scopes, known limitations, demo data seeding, CLI commands, benchmark runs, and deployment notes.
A new developer should be able to run the demo from a clean checkout using only README and .env.example.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-S4-01 | P0 | Freeze DB schema | migrations + `docs/audit-schema.md` | No planned schema-breaking changes after this sprint |
| DE-S4-02 | P0 | Finalize connector config | connector docs + settings | All connector env vars documented |
| DE-S4-03 | P0 | Finalize browser tool contract | `docs/browser-tool-contract.md` | Defines request/response/error behavior for each browser tool |
| DE-S4-04 | P0 | Finalize API executor contract | `docs/api-executor-contract.md` | Defines tool routing and connector response behavior |
| DE-S4-05 | P0 | Add seed demo data script | `scripts/seed_demo.py` | Demo scenarios can run from seeded state |
| DE-S4-06 | P0 | Add benchmark script | `scripts/run_benchmark.py` | Outputs P50/P95 raw vs guarded measurements |
| DE-S4-07 | P0 | Add reproducibility doc | `docs/reproducibility.md` | Clean setup instructions are complete |
| DE-S4-08 | P1 | Add CLI packaging | package config | CLI/demo commands are installable or documented |
| DE-S4-09 | P1 | Add deployment notes | `docs/deployment.md` | Local and demo deployment steps covered |

### Exit criteria

A new developer can run setup, seed data, run a scenario, export audit, and run benchmark from documentation.

---

## Showcase — Architecture and Demo Materials

**Dates:** 2026-09-14 to 2026-09-20  
**Sprint goal:** Prepare architecture diagrams, artifact flow, connector narrative, reproducibility notes, and fallback demo modes.

### Deliverables

- Architecture diagram.
- Artifact flow diagram.
- Connector narrative.
- Live demo mode.
- Mock/sandbox demo mode.
- Trace replay mode.
- Final demo artifacts.

### Codex task prompt

```text
Prepare AgentGate DE showcase materials.
Create Markdown Mermaid diagrams for backend architecture and artifact flow.
Document the connector narrative: when API executor is used, when browser executor is used, and how AgentGate gates both paths.
Add fallback demo and trace replay mode so the showcase does not depend entirely on live external APIs.
Verify demo artifacts are reproducible.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-SC-01 | P0 | Create architecture diagram | `docs/architecture.md` | Shows Web/CLI → loop → core → executor → audit/dashboard |
| DE-SC-02 | P0 | Create artifact flow diagram | `docs/artifact-flow.md` | Shows ActionRequest → DecisionResponse → ExecutionResult → AuditEvent |
| DE-SC-03 | P1 | Write connector narrative | `docs/connectors/overview.md` | Explains API vs browser execution policy |
| DE-SC-04 | P0 | Add trace replay mode | `scripts/run_demo_scenario.py --replay` | Demo can run from stored trace artifacts |
| DE-SC-05 | P0 | Add fallback demo mode | config/scripts | Demo can run without external APIs |
| DE-SC-06 | P1 | Package demo artifacts | `artifacts/showcase/` | Includes traces, screenshots, benchmark sample |

### Exit criteria

The showcase can run in live mode, mock mode, or replay mode.

---

## Testing — Public Testing Support

**Dates:** 2026-09-21 to 2026-09-24  
**Sprint goal:** Monitor testing logs, categorize connector/browser failures, preserve artifacts, and support evaluation.

### Deliverables

- Test run IDs.
- Exported audit logs.
- Exported traces.
- Exported benchmark results.
- Failure category summary.
- Updated artifact manifest.

### Codex task prompt

```text
Support AgentGate public testing from the Data Engineer side.
Ensure every test run has run_id and exportable artifacts.
Add or improve scripts for exporting audit logs, action traces, benchmark outputs, error summaries, screenshots, and configs.
Categorize failures into connector auth, browser selector, policy/decision mismatch, audit write failure, timeout, and service contract mismatch.
Update the artifact manifest after each test run.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-T-01 | P0 | Ensure run IDs for all tests | core/tracing | Every test run has stable `run_id` |
| DE-T-02 | P0 | Export audit logs | `scripts/export_audit.py` | JSONL export works by run/date/latest |
| DE-T-03 | P0 | Export traces | `scripts/export_traces.py` | JSONL export includes full action chain |
| DE-T-04 | P0 | Export benchmarks | `scripts/run_benchmark.py` output | Benchmark JSON/Markdown generated |
| DE-T-05 | P0 | Add failure categorization | `scripts/summarize_failures.py` | Groups errors by category and connector/tool |
| DE-T-06 | P1 | Preserve screenshots | artifacts folder | Browser screenshot metadata and files linked to run ID |
| DE-T-07 | P0 | Update artifact manifest | `artifacts/manifest.json` | Manifest includes config, schema, commit, traces, logs |

### Exit criteria

Testing data is reproducible and usable by DA/DS for metrics and failure analysis.

---

## Fixing — Critical Bug Fixes

**Dates:** 2026-09-25 to 2026-09-28  
**Sprint goal:** Patch only launch-critical connector, browser, audit, service contract, and reproducibility bugs.

### Bug severity policy

| Severity | Definition | Fix during this sprint? |
|---|---|---|
| P0 | Demo cannot run, unsafe action can execute silently, audit missing critical path | Yes |
| P1 | Demo-critical connector/browser/audit path unreliable | Yes |
| P2 | Usability issue or confusing output with workaround | Maybe |
| P3 | Polish or new feature | No |

### Codex task prompt

```text
Patch only critical AgentGate DE issues.
Focus on connector failures, browser executor edge cases, database logging bugs, service contract mismatches, and reproducibility failures.
Do not introduce new features or schema-breaking changes unless required to fix a P0 safety or demo blocker.
Retest demo-critical flows after each fix.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-FX-01 | P0 | Patch connector blockers | connector modules | Demo-critical connector paths pass |
| DE-FX-02 | P0 | Patch browser edge cases | browser/executor modules | Demo-critical browser paths pass |
| DE-FX-03 | P0 | Patch audit logging bugs | audit modules | Audit completeness check passes |
| DE-FX-04 | P0 | Patch service contract mismatches | schemas/routes | FE/DS integration no longer breaks |
| DE-FX-05 | P0 | Patch reproducibility failures | scripts/docs | Clean setup works from docs |
| DE-FX-06 | P1 | Retest final demo flows | test report | All launch-critical flows are green |

### Exit criteria

No P0/P1 issues remain open.

---

## Final — Release Freeze

**Dates:** 2026-09-29 to 2026-10-01  
**Sprint goal:** Freeze connector versions, Playwright executor, docs, database schema, deployment scripts, reproducibility checklist, benchmark artifacts, and final manifest.

### Deliverables

- Release candidate tag.
- Frozen dependency versions.
- Final database schema.
- Final docs.
- Final deployment scripts.
- Final reproducibility checklist.
- Final benchmark artifacts.
- Final artifact manifest.

### Codex task prompt

```text
Prepare AgentGate DE release freeze.
Lock connector versions, dependencies, Playwright executor behavior, database schema, API/browser docs, deployment scripts, benchmark artifacts, and reproducibility checklist.
Create a release candidate tag note and final artifact manifest.
Run clean-machine setup verification or simulate it in CI/scripts.
Do not make non-critical changes.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-RF-01 | P0 | Lock dependencies | lockfile/requirements | Versions pinned |
| DE-RF-02 | P0 | Freeze DB schema | migrations/docs | No pending migration changes |
| DE-RF-03 | P0 | Freeze executor behavior | docs + code | Browser/API contracts match implementation |
| DE-RF-04 | P0 | Finalize deployment scripts | scripts/docs | Setup/deploy commands work |
| DE-RF-05 | P0 | Finalize benchmark artifacts | artifacts/benchmarks | Raw-vs-guarded results available |
| DE-RF-06 | P0 | Finalize manifest | `artifacts/manifest.json` | Lists traces, configs, docs, commit, schema versions |
| DE-RF-07 | P0 | Tag release candidate note | `docs/release-candidate.md` | Includes known limitations and run commands |
| DE-RF-08 | P0 | Clean setup verification | checklist/log | Fresh clone path verified |

### Exit criteria

Release candidate is reproducible and no schema or executor changes are planned.

---

## Launch — Public Release

**Date:** 2026-10-02  
**Sprint goal:** Publish runnable and auditable project package.

### Deliverables

- Published repository structure.
- Published connector docs.
- Published deployment notes.
- Published artifact manifest.
- Final build package.
- Known limitations.
- Launch technical support.

### Codex task prompt

```text
Prepare final AgentGate DE launch package.
Ensure the repository is clean, docs are complete, connector setup is documented, deployment notes are accurate, artifact manifest is final, known limitations are clear, and the runnable demo package is archived.
Verify final commands for setup, seed, scenario run, benchmark, audit export, and trace export.
```

### Backlog

| ID | Priority | Task | Output | Acceptance criteria |
|---|---:|---|---|---|
| DE-L-01 | P0 | Publish final repo structure | repository | No temporary/debug files in main paths |
| DE-L-02 | P0 | Publish connector docs | `docs/connectors/` | Setup, env vars, scopes, limitations documented |
| DE-L-03 | P0 | Publish deployment notes | `docs/deployment.md` | Local/demo deployment clear |
| DE-L-04 | P0 | Publish artifact manifest | `artifacts/manifest.json` | All release artifacts listed |
| DE-L-05 | P0 | Archive final build package | release artifact | Build/demo package recoverable |
| DE-L-06 | P0 | Publish known limitations | README/docs | Scope gaps and connector limitations explicit |
| DE-L-07 | P1 | Support technical Q&A | notes | Architecture, connector, audit, reproducibility questions answerable |

### Exit criteria

The public release can be run, inspected, and evaluated by someone outside the original team.

---

# 6. Recommended Codex Execution Order

Use this order when asking Codex to code. It is more implementation-safe than the calendar order.

```text
1. Create schemas and tests.
2. Create audit DB and append-only repository.
3. Create ActionRequest builder.
4. Create local file connector.
5. Create Playwright browser open/snapshot + selector_map.
6. Create selector revalidation.
7. Create one end-to-end vertical slice.
8. Add GitHub connector.
9. Add Gmail connector or mock/sandbox Gmail path.
10. Add traces and latency instrumentation.
11. Add FE/DA-facing read endpoints.
12. Add benchmark scripts.
13. Stabilize connector/browser errors.
14. Package reproducible demo.
```

---

# 7. Suggested Initial Codex Prompt

Use this as the first high-level prompt if the repo is still empty or messy.

```text
You are the coding agent for AgentGate's Data Engineer workstream.
Read sprint.md and implement the project foundation first.
Do not skip ahead to optional connectors.
Start with schemas, audit logging, ActionRequest pipeline, local file connector, Playwright snapshot builder, selector_map, and one end-to-end vertical slice.
Every public contract must include schema_version.
Every action must propagate run_id and action_id.
Do not log raw secrets, tokens, credentials, private file contents, or full sensitive payloads.
Keep browser selector_map server-side; expose only snapshot-scoped element_id to planner-facing objects.
After each implementation step, add tests and update README/docs if commands or config changed.
```

---

# 8. Minimal Release Command Target

By launch, these commands or equivalents should work:

```bash
cp .env.example .env
python scripts/seed_demo.py
python scripts/run_demo_scenario.py booking_payment_message --mode mock
python scripts/run_demo_scenario.py code_protection --mode mock
python scripts/run_demo_scenario.py productivity_archive --mode mock
python scripts/run_benchmark.py
python scripts/export_audit.py --latest
python scripts/export_traces.py --latest
```

Optional Docker path:

```bash
cp .env.example .env
docker compose up --build
```
