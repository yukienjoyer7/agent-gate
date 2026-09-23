# Merged AgentGate guardrail

This project embeds the guardrail core from
[NafisNaufal/agentgate](https://github.com/NafisNaufal/agentgate/tree/9b919c604d2a0955fff26b4f05045281514c0220),
revision `9b919c604d2a0955fff26b4f05045281514c0220`, version 0.3.0.
The current CLI, FastAPI routes, planner, connectors, approvals, and final audit
stores remain the application runtime. The embedded code is under
`app/domains/guardrail/_vendor/agentgate`; provenance is recorded in its `NOTICE.md`.

Both repositories publish a distribution and console command named `agentgate`.
Embedding the pinned core avoids a self-dependency or replacing this project's
CLI. Install only this project; do not install the other distribution alongside it.

## Setup

The default backend is `GUARDRAIL_BACKEND=agentgate`. Its detector is independent
of the planner provider/API key. Install Ollama, then:

```powershell
ollama pull qwen2.5:7b
ollama pull gemma-4-E2B-it
$env:OLLAMA_NUM_PARALLEL = "6"
ollama serve
```

If Ollama already runs, set parallelism in its service configuration instead of
starting a second process. POSIX equivalent: `OLLAMA_NUM_PARALLEL=6 ollama serve`.
The engine dispatches six requests concurrently, but server capacity determines
whether inference actually overlaps.

| Setting | Default | Purpose |
| --- | --- | --- |
| `GUARDRAIL_BACKEND` | `agentgate` | `agentgate` or explicit `legacy` rollback |
| `OLLAMA_HOST` | `http://localhost:11434` (`http://host.docker.internal:11434` in Docker Desktop) | Detector endpoint; remote hosts require HTTPS |
| `AGENTGATE_LLM_DETECTOR_MODEL` | `qwen2.5:7b` | Primary local detector model |
| `AGENTGATE_LLM_FALLBACK_MODEL` | `gemma-4-E2B-it` | Fallback model used only after a primary timeout |
| `AGENTGATE_LLM_FALLBACK_ATTEMPTS` | `1` | Maximum fallback requests; bounded to 0 or 1 |
| `AGENTGATE_LLM_DETECTOR_TIMEOUT` | `30` | Seconds per detector request |
| `AGENTGATE_DETECTOR_ARCHITECTURE` | `six` | Six detectors or upstream experimental `unified` |
| `GUARDRAIL_AUDIT_PATH` | `artifacts/audit/guardrail.jsonl` | Server evaluation journal |

The Compose setup points the API container at `http://host.docker.internal:11434`
when Ollama is running on the Docker Desktop host. This reserved local bridge
hostname is allowed for HTTP; arbitrary remote HTTP detector endpoints remain
rejected and must use HTTPS.

The server reads these from its normal settings/`.env`. The CLI reads supported
shell overrides and always writes its evaluation journal to
`<AGENTGATE_DATA_DIR>/guardrail.jsonl` (or the platform's default private data
directory). It does not load project `.env` files. `agentgate doctor` checks the
Ollama model list without generating text, pulling models, or executing actions.

Ordinary setup/help/history operations need no Ollama. A run whose detector is
unavailable pauses for approval; non-interactive runs return interaction-required
status. The integration does not silently permit actions or select the old backend.

## Evaluation and enforcement

```text
Existing planner / API proposal
  -> application ActionRequest and trusted connector metadata
  -> upstream PII / secrets / source code / payment-phishing /
     prompt-injection / action-intent detectors
  -> upstream JSON policy packs and risk scoring
  -> existing application policy floor and structured redaction
  -> durable evaluation journal
  -> existing approval / clarification / executor flow
  -> existing final action audit and trace
```

The adapter scans the complete payload, including nested values, even when the
planner supplies a harmless summary. It translates `booking` to `booking_style`,
connector names to upstream target-system names, and single risk hints to lists.
Trusted connector operations supply additional risk and rollback metadata;
unknown operations and incompatible action types are blocked. Existing rules
for payments, destructive actions, and required Calendar fields remain enforced.

Both synchronous `decide` and asynchronous `adecide` use the selected backend.
The async entry point runs the blocking upstream engine in a worker thread so
detector calls do not block CLI events or the API event loop. Legacy browser
prototype batches are mapped to their most consequential action type.

The five decision values retain their meanings:

| Decision | Application behavior |
| --- | --- |
| `ALLOW` | Execute through the existing router |
| `BLOCK` | Do not execute; cannot be approved around |
| `NEED_APPROVAL` | Wait for approval of the exact step |
| `ASK_USER` | Collect clarification and re-evaluate; clarification is not approval |
| `SANITIZE` | Substitute permitted content fields, re-evaluate, and request confirmation |

Structured redaction walks content values without changing IDs, destinations,
paths, amounts, or field types. Compound browser fills and selects are rewritten
inside their validated action list. Secret material that cannot be rewritten into a
supported content field is blocked, so later approval cannot release its original
value. The router rejects a SANITIZE decision with no replacement payload. Detector
prompts redact recognized credential fields and patterns before the Ollama request;
the deterministic host floor preserves the corresponding secret-egress block. A
detector error holds the action without execution while preserving any successful
detector's BLOCK finding. Required clarification remains a prerequisite; after it
is supplied, a detector error requires approval.

## Audit integration

Upstream requires an audit write before returning a decision. The injected host
store meets that requirement using a locked, flushed, fsynced JSONL evaluation
journal instead of introducing a second PostgreSQL schema. This preserves the
local CLI's independence from PostgreSQL. Mount/persist the configured journal
directory when deploying the server.

Every evaluation records a unique ID, run/action IDs, timestamp, sanitized
structured request, tool identity, and final mapped decision. Re-evaluation after
input/redaction produces another entry for the same action. A failed write raises
`AuditUnavailable` before execution. Final SQLite/PostgreSQL/JSONL action audits
remain write-once and reference `guardrail_audit_id`; there is no database migration.

This merge imports pre-action evaluation, not the upstream agent loop. Its task,
terminal-message, and executor-observation screening stages are not transplanted;
the existing runtime's observation/output sanitization still applies. Upstream
executors, planner, scenario commands, and benchmarks are also outside this merge.

## Verification and maintenance

```bash
pytest tests/integration/test_agentgate_guardrail.py tests/upstream_guardrail
pytest
```

Integration tests mock only detector transport and exercise real policy/scoring,
redaction, failure handling, async dispatch, API/local runtime wiring, and durable
audit behavior. Imported upstream tests cover scan text, concurrent detection,
and malformed responses. Existing regression tests explicitly select the legacy
backend and isolate settings from developer credentials.

`scripts/smoke_cli_package.py` tests a base-only wheel from outside the checkout,
using local HTTP stubs for both planner and Ollama. It verifies bundled policies,
doctor, a file run, evaluation/final audit linkage, history, and secret masking.
It does not measure live model accuracy or latency.

To update upstream, compare the files listed in `NOTICE.md` against a new pinned
revision, retain the host audit shim, and rerun both suites and the wheel smoke.
The vendored source is excluded from host formatting/linting and mypy errors so
its contents stay comparable with the original source.

When a primary Qwen request times out, the detector logs the timeout, sends an
Ollama unload request with `keep_alive: 0`, waits for that response, and only then
sends one request to the fallback Gemma model. An unload failure prevents the
fallback request and is reported as a guardrail detector failure. A fallback
failure is also bounded and never loops indefinitely.
