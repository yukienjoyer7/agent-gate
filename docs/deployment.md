# Deployment and operations

## Topologies

| Topology | Components | Notes |
|----------|------------|-------|
| Local CLI | `agentgate` + Ollama | Single user, no server |
| Single host | API (uvicorn) + PostgreSQL + Ollama | Simplest server deployment |
| Docker Compose | `api`, `postgres`; Ollama on host | Provided `docker-compose.yml` |

The design is a single deployable unit. Horizontal scaling (Redis queue and workers) is planned in ADR 0001
but not implemented. Because run state and OAuth `state` are in process memory, run **one API worker**.

## Container

- `Dockerfile` is multi-stage: it builds a venv with `.[server,browser,stripe]`, installs Chromium and its
  system libraries, runs as non-root `appuser` (uid 1000), and starts `uvicorn app.main:app` on port 8000.
- `docker-compose.yml`: `postgres` (`pgvector/pgvector:pg17`, volume `postgres_data`, healthcheck) and `api`
  (`.env`, `host.docker.internal` mapped to the host gateway for Ollama). The repo is bind-mounted at `/app` in
  the provided Compose file, which suits development; remove the mount for production images.
- The Compose file reads `DB_PASSWORD` (default `agentgate`). Set your own, and set the same password in
  `DATABASE_URL`: the API's default URL hardcodes `agentgate`. Also make sure `.env` does not point `DATABASE_URL` at
  `localhost` (Compose interpolates `.env`, which overrides the service-name default).
- `alembic.ini` is not copied into the image. `docker compose exec api alembic upgrade head` works with the
  provided bind mount; a production image without the mount must copy `alembic.ini` or run migrations elsewhere.
- **Persist the guardrail journal.** ADR 0003 requires server deployments to keep the evaluation journal
  (`GUARDRAIL_AUDIT_PATH`, default `artifacts/audit/guardrail.jsonl`). The provided Compose file only preserves it
  because the repo is bind-mounted. In a production image, mount a volume for `artifacts/` (and `data/` for
  screenshots), otherwise the journal is lost with the container.

```bash
docker compose up --build -d
docker compose exec api alembic upgrade head
docker compose logs -f api
```

## Release checklist

1. `pytest` passes (see [testing](testing.md)).
2. `ruff check .`, `black --check .`, `mypy app` pass.
3. Migrations reviewed; `alembic upgrade head` tested on a copy of production data.
4. `.env` for the target environment: `APP_ENV`, `DATABASE_URL`, `LLM_*`, `AUDIT_BACKEND=postgres`,
   webhook secrets, `OLLAMA_HOST`, connector credentials.
5. Ollama reachable with the detector model pulled.
6. Health check `GET /api/v1/health` returns ok.
   The evaluation journal path is writable and on a persistent volume (a failed journal write blocks execution).
7. One guarded run of each critical path (file read, an approval, a blocked action).

## Migrations

```bash
alembic upgrade head                       # apply
alembic current                            # show revision
alembic revision --autogenerate -m "msg"   # after model changes (models are imported in migrations/env.py)
alembic downgrade -1                       # roll back one
```

`downgrade` of `0001` drops `audit_logs` and its trigger, which **destroys the audit history**. Never run it on
production without a backup. For remote databases set `DATABASE_SSL_MODE`; `scripts/check_database_connection.py`
tests connectivity without printing credentials.

## Monitoring

| Signal | How |
|--------|-----|
| Liveness | `GET /api/v1/health` |
| Logs | Structured JSON to stdout (`python-json-logger`); set `LOG_LEVEL` |
| Throughput / latency | `GET /api/v1/benchmark`; `latency` fields on audit records; JSONL traces |
| Decisions | Count of `BLOCKED`, `PENDING_APPROVAL`, `FAILED` in `audit_logs.execution_status` |
| Guardrail health | `guardrail.jsonl`; `evaluation_error` in decisions; `agentgate doctor` for Ollama |
| Pending approvals | `GET /api/v1/approvals` |

Useful queries:

```sql
SELECT execution_status, count(*) FROM audit_logs GROUP BY 1 ORDER BY 2 DESC;
SELECT run_id, action_id, error_type, created_at FROM audit_logs
 WHERE execution_status = 'FAILED' ORDER BY created_at DESC LIMIT 50;
```

## Backup and restore

- **PostgreSQL:** `pg_dump -Fc agentgate > agentgate.dump`; restore with `pg_restore`. The immutability
  trigger is part of the schema; verify it after a restore.
- **JSONL:** copy `artifacts/audit/` (includes `guardrail.jsonl`) and `artifacts/traces/` (both gitignored).
- **CLI:** back up `AGENTGATE_DATA_DIR` (`state.sqlite3`, `guardrail.jsonl`) and `config.toml`.
- Include `oauth_tokens` in the same protection level as the credentials it contains.

## Runbook

| Symptom | Check | Action |
|---------|-------|--------|
| Every run pauses for approval | `agentgate doctor`; is Ollama up and the model pulled? | Start Ollama / `ollama pull qwen2.5:7b` / `ollama pull gemma-4-E2B-it`; check `OLLAMA_HOST` (Docker: `host.docker.internal`) |
| Detector timeouts | `AGENTGATE_LLM_DETECTOR_TIMEOUT`, `OLLAMA_NUM_PARALLEL` | Raise timeout, set parallelism to 6, verify both Qwen and Gemma are pulled, or use a lighter model |
| Guardrail queue unavailable | `docker compose ps redis`; `redis-cli ping` | Restore Redis; actions fail closed to review while the queue is unavailable |
| Guardrail queue delay | Redis list `<queue-name>:waiting`; Ollama `/api/ps` | Let the active CPU inference finish, reduce request concurrency, or use a faster model/GPU |
| 5xx on `/audits`, `/runs` | DB reachable? `AUDIT_BACKEND` | Fix `DATABASE_URL`, or switch to `jsonl` temporarily |
| Migration fails on TLS | Driver in `DATABASE_URL`; `DATABASE_SSL_MODE` | `postgresql+asyncpg`: `DATABASE_SSL_MODE=require`. `postgresql+psycopg`: add `?sslmode=require` to the URL |
| OAuth callback error | Single worker? redirect URI exact match? | Restart flow within seconds; align redirect URI with the provider console |
| Telegram webhook 403/503 | `TELEGRAM_WEBHOOK_SECRET` set and equal to the registered secret | Re-run `scripts/set_telegram_webhook.py` |
| Stripe webhook 400 | `STRIPE_WEBHOOK_SECRET`, clock skew | Fix secret; tolerance is `STRIPE_WEBHOOK_TOLERANCE_SEC` |
| Browser actions fail in container | Chromium installed? memory? | Rebuild image; reduce `BROWSER_MAX_CONCURRENT_SESSIONS` |
| Approval never arrives | `AGENT_WAIT_RESPONSE_TIMEOUT_SEC` (600) | Respond in time or raise the timeout |
| Restart lost live runs | Expected: run state is in memory | Re-run; audit rows remain |
| Disk fills | `artifacts/` and screenshots grow (no cleanup job) | Rotate manually |

## Rollback

- Code: redeploy the previous image or commit.
- Guardrail: `GUARDRAIL_BACKEND=legacy` (deliberate, documented rollback; weaker detection).
- Audit backend: `AUDIT_BACKEND=jsonl` if PostgreSQL is unavailable (history splits across stores).
- Schema: prefer forward fixes; see the downgrade warning above.
