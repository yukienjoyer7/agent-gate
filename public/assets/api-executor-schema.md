# API Executor Schema

## Input

`APIExecutor.execute()` accepts an `ActionRequest`.

Required fields:

```json
{
  "schema_version": "0.1",
  "run_id": "run_...",
  "action_id": "act_...",
  "action_type": "FILE_READ",
  "target_system": "local_file",
  "target": "sample.txt",
  "payload": {
    "action": "read",
    "path": "sample.txt"
  }
}
```

`payload.action` is required. `target_system` selects the connector.

Supported connector targets:

| target_system | Current mode |
|---|---|
| `local_file` | safe allowlisted read |
| `github` | mock |
| `gmail` | mock |

## Output

The executor returns an `ExecutionResult`.

```json
{
  "schema_version": "0.1",
  "run_id": "run_...",
  "action_id": "act_...",
  "executor": "local_file",
  "status": "SUCCESS",
  "result_summary": "Read sample.txt (86 chars)",
  "data": {},
  "error": null,
  "latency_ms": 0,
  "created_at": "ISO-8601 timestamp"
}
```

Failures use normalized connector errors:

```json
{
  "status": "FAILED",
  "error": {
    "code": "VALIDATION",
    "message": "missing connector action",
    "retryable": false,
    "details": {}
  }
}
```
