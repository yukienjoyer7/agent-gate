# Known limitations and inconsistencies

Findings from reading the repository. **Verified** means confirmed in code. **Unverified** means not confirmed
and worth checking before relying on it.

## Audit and approvals

- **Action-sourced audit only** (verified). The event-sourced v4 spec is not implemented ([audit-design](audit-design.md),
  [ADR 0004](decisions/0004-audit-granularity.md)).
- **No approval timeline** (verified). The approval outcome is stored on the final row; when approval was
  requested and decided is not recorded as separate events.
- **`PENDING_APPROVAL` snapshots** (verified). `POST /actions/run` writes a pending row that nothing resumes;
  `action_id` is unique so it cannot be superseded.
- **Timeouts write a `FAILED` row** (verified). If a paused step times out waiting for a response, the run is
  marked `failed` and a `FAILED` audit row ("timed out waiting for user response") is written.
- **Audit write failures can be swallowed** (verified). For declined, blocked, skipped and timed-out steps the loop
  logs a warning and continues if the audit write fails.
- **Traces are partial** (verified). `ActionTrace` records are written only by `run_guarded_action`; browser
  batches and blocked, declined or timed-out chat steps have audit rows but no trace.
- **Browser batches** (verified). By default consecutive browser steps produce one combined row; enable
  `ATOMIC_BROWSER_AUDIT` for per-step rows.
- **Live run state is in memory** (verified). It does not survive a restart.

## Configuration and setup

- **Database driver** (verified): `.env.example` uses `asyncpg`, code default and Compose use `psycopg`. Both
  are supported; keep them consistent.
- **`GUARDRAIL_LLM_ENABLED` default** (verified): `True` in code (only affects the legacy backend) but `False` in
  `.env.example`, and the code comment says "defaults to OFF".
- **Retention settings do nothing** (verified): `AUDIT/TRACE/SCREENSHOT_RETENTION_DAYS` are defined and set per
  profile, but no code reads them. There is no cleanup job. ADR 0002 explicitly defers automated retention.
- **Unused setting** (verified): `XENDIT_API_KEY` is defined with no code using it.
- **Redis** (verified): `redis` is a dependency and ADR 0001 lists a queue layer, but the Compose file has no Redis
  service and no Redis code path was found.
- **Missing file** (verified): the README links `AgentGate Technical Foundation Document.md`, which is not in
  the repository. The old README also says "Sprint 1"; `sprint.md` describes a `backend/...` layout that differs
  from the real `app/...` layout.
- **Playwright is a hard import for the server** (verified): `app.main` imports it at startup, so the `browser`
  extra is required to run the server or its tests. The local CLI imports it lazily.
- **Compose `.env` interpolation** (from reading the Compose file, not run): a copied `.env` defining `DATABASE_URL`
  with `localhost` overrides the Compose default; see [getting-started](getting-started.md#path-c-docker-compose-api--postgresql).
- **`DATABASE_SSL_MODE`** (verified): applies only to `postgresql+asyncpg` URLs.
- **Windows event loop** (verified): use `python run.py` so Playwright can spawn a subprocess.

## Security

- No API authentication or authorization; CORS is `*`.
- OAuth tokens are stored unencrypted; OAuth `state` is in process memory (single worker only).
- `.env.development` and `.env.staging` are tracked in git.
- Full list and mitigations: [security](security.md).

## Product scope

- **Connector breadth** (verified): Gmail is read-only (`list_messages`), GitHub is `repo_metadata` only, Calendar
  has list/create, Local files are read-only. There is no email send, GitHub write, or calendar update/delete.
- **Guardrail depends on Ollama** (verified): without it every guarded run pauses for approval. Intentional
  fail-safe, but an outage halts autonomy.
- **Data leaves the machine** (verified in CLI docs): prompts and observations go to the configured LLM provider,
  and connectors call external APIs.
- **One active CLI run per profile** (verified in CLI docs); no daemon, background execution, `serve`, or resume.
- **Detector quality** (unverified): accuracy of the six LLM detectors on `qwen2.5:7b` has not been measured in
  the docs reviewed; there are no evaluation results or benchmark reports in the repo.
- **Live provider behaviour** (unverified): connectors have unit tests with fakes; behaviour against real
  Google, GitHub, Telegram, and Stripe accounts was not exercised while writing these docs.
- **Benchmark endpoint** (verified): `/benchmark` averages recorded latencies only. There is no raw-vs-guarded
  comparison harness in the repo.

## Testing

- `tests/e2e/` and `tests/fixtures/` are empty placeholders.
- No CI configuration was found in the repository.
- Migrations are not exercised against PostgreSQL by the default test suite.
