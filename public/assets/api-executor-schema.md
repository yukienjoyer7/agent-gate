<<<<<<< HEAD
# API Executor Schema

The API executor is the guarded path for non-browser actions. It receives a
validated `ActionRequest`, checks that the request names a connector action,
routes the request to the connector selected by `target_system`, and returns a
normalized `ExecutionResult`.

The executor does not decide whether an action is safe. Safety decisions happen
before this step in the guardrail layer. The executor only runs actions that the
router sends to it, and it always returns a structured result instead of raising
raw connector errors to callers.

## Flow

```text
Raw tool proposal
      |
      v
build_action_request()
      |
      v
ActionRequest
  schema_version
  run_id
  action_id
  action_type
  target_system
  target
  payload[action, ...]
      |
      v
Guardrail decision
      |
      v
DecisionResponse
  ALLOW / BLOCK / NEED_APPROVAL
      |
      v
ExecutionRouter
      |
      v
APIExecutor
      |
      +--> validate payload.action exists
      |
      +--> choose connector by target_system
             |
             +--> local_file -> LocalFileConnector
             +--> github     -> GitHubConnector repo_metadata
             +--> gmail      -> GmailConnector mock
      |
      v
ExecutionResult
  schema_version
  run_id
  action_id
  executor
  status
  result_summary
  data
  error
  latency_ms
      |
      v
AuditRepository.write()
      |
      v
AuditEvent JSONL
      |
      v
ActionTrace JSONL
```

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

GitHub repo metadata example:

```json
{
  "user_goal": "Inspect repository metadata",
  "action_type": "API_CALL",
  "target_system": "github",
  "target": "yukienjoyer7/agent-gate",
  "payload": {
    "action": "repo_metadata",
    "owner": "yukienjoyer7",
    "repo": "agent-gate"
  }
}
```

Post it to:

```text
POST /api/v1/actions/run
```

Supported connector targets:

| target_system | Current mode |
|---|---|
| `local_file` | safe allowlisted read |
| `github` | read-only repo metadata |
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
=======
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
>>>>>>> bb636b9 (feat: adding browser executor demo file and additional recovery feature)
