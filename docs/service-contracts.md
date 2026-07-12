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
| GET | `/api/v1/benchmark` | Return current latency summary |

## CLI

```bash
python scripts/run_demo_scenario.py local_file_read
python scripts/run_demo_scenario.py browser_snapshot
python scripts/export_audit.py --latest
python scripts/export_traces.py --latest
```

Audit JSONL defaults to `artifacts/audit/events.jsonl`.
Trace JSONL defaults to `artifacts/traces/actions.jsonl`.
