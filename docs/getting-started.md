# Getting started

Pick a path. **A (CLI)** is the fastest. **B (server)** gives you the HTTP API. **C** runs the server in Docker.

## 0. Prerequisites

| Requirement | Needed for | Check |
|-------------|-----------|-------|
| Python 3.11+ | everything | `python --version` |
| An LLM API key | the planner (OpenAI-compatible, Anthropic, or Gemini dialect) | see `LLM_*` in [configuration](configuration.md) |
| [Ollama](https://ollama.com) | the default guardrail's detectors | `ollama --version` |
| Docker + Compose | server with bundled PostgreSQL (optional) | `docker compose version` |
| PostgreSQL 17 | server with `AUDIT_BACKEND=postgres` | via Docker or your own |

### Prepare the guardrail model (all paths)

The default guardrail (`GUARDRAIL_BACKEND=agentgate`) runs six detectors on a local Ollama model.

```bash
ollama pull qwen2.5:7b
OLLAMA_NUM_PARALLEL=6 ollama serve        # PowerShell: $env:OLLAMA_NUM_PARALLEL = "6"
```

If Ollama is unreachable, guarded runs **pause for approval** instead of silently allowing actions.
`GUARDRAIL_BACKEND=legacy` is an explicit rollback to the older rule engine.

## What pip installs

`pip install .` installs only the core packages. Everything else is an optional extra:

| Extra | Adds | Needed for |
|-------|------|-----------|
| *(core)* | sqlalchemy, pydantic, pydantic-settings, httpx, PyYAML, platformdirs, filelock, keyring, python-json-logger | CLI and shared code |
| `server` | fastapi, uvicorn, alembic, psycopg, asyncpg, redis | HTTP API, migrations |
| `browser` | playwright | Browser actions |
| `stripe` | stripe[async] | Stripe connector |
| `dev` | pytest, pytest-asyncio, ruff, black, mypy, aiosqlite | Tests and linting |

Not installed by pip: Chromium (`playwright install chromium`), Ollama, PostgreSQL, Docker.

---

## Path A: Local CLI

No server, Docker, PostgreSQL, or Redis.

```bash
pipx install .                 # from the repo root
agentgate init                 # LLM dialect, endpoint/model, timezone, workspace, credential store
agentgate doctor               # credentials, workspace, storage, browser, Ollama readiness
agentgate run "Read README.md"
agentgate history
agentgate show <run-id>
```

Headless setup:

```bash
agentgate init --non-interactive --workspace /absolute/path/to/project --credential-store env
export LLM_API_KEY=...         # supply secrets through the environment, never CLI flags
```

Optional:

```bash
agentgate connect github | gmail | calendar | stripe | llm
agentgate setup browser        # downloads Chromium, enables browser actions
agentgate payments sync        # refresh tracked Stripe checkout/refund status
```

The CLI does **not** read a project `.env`. Full guide: [cli.md](cli.md).

---

## Path B: Server (FastAPI)

### 1. Install

```bash
python3.11 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev,server,browser,stripe]"
python -m playwright install chromium                       # if you use browser actions
```

Or with uv: `uv sync --extra dev --extra server --extra browser --extra stripe`.

### 2. Configure

```bash
cp .env.example .env
```

Minimum edits:

| Variable | Set it to |
|----------|-----------|
| `LLM_URL`, `LLM_MODEL`, `LLM_API_KEY` | Your provider's endpoint, model, key |
| `DATABASE_URL` | Your PostgreSQL URL |
| `AUDIT_BACKEND` | `postgres`, or `jsonl` to run with no database |

> `.env.example` uses `postgresql+asyncpg://`; the code default and `docker-compose.yml` use
> `postgresql+psycopg://`. Both drivers are installed with the `server` extra and both work. Keep `.env`
> and Compose consistent.

**Quickest setup with no database:** `AUDIT_BACKEND=jsonl`. Audit events go to
`artifacts/audit/events.jsonl`, traces to `artifacts/traces/actions.jsonl`.

### 3. Start PostgreSQL (skip if `AUDIT_BACKEND=jsonl`)

```bash
docker compose up -d postgres
```

### 4. Migrate

```bash
alembic upgrade head
```

Creates `audit_logs` (immutable via trigger), `oauth_tokens`, `telegram_contacts`, `stripe_payments`,
`stripe_webhook_events`. See [data-model.md](data-model.md).

### 5. Start the API

```bash
uvicorn app.main:app --reload
python run.py                   # Windows: sets the event-loop policy Playwright needs
```

### 6. Verify

```bash
curl http://localhost:8000/api/v1/health       # {"status":"ok"}
curl http://localhost:8000/                    # service, env, version
```

Swagger UI: <http://localhost:8000/docs>.

### 7. Try a guarded run

```bash
curl -X POST http://localhost:8000/api/v1/chat/execute \
  -H "Content-Type: application/json" -d '{"prompt":"Read file sample.txt"}'
# -> {"run_id":"run_...","status":"running","prompt":"..."}

curl http://localhost:8000/api/v1/chat/execute/<run_id>
curl "http://localhost:8000/api/v1/audits?run_id=<run_id>"
```

If a step needs approval:

```bash
curl -X POST http://localhost:8000/api/v1/chat/execute/<run_id>/respond \
  -H "Content-Type: application/json" -d '{"step_index":0,"action":"approve"}'
```

A static browser demo lives in `fe/agent-gate-demo.html` (CORS is open for it).

---

## Path C: Docker Compose (API + PostgreSQL)

```bash
cp .env.development .env         # or .env.example
docker compose up --build
docker compose exec api alembic upgrade head
```

The image installs `.[server,browser,stripe]` and Chromium. Compose defines `postgres` and `api`; the API
listens on port 8000. The container reaches Ollama on the host at `http://host.docker.internal:11434`, so
start Ollama on the host first.

---

## Scripts

```bash
python scripts/run_demo_scenario.py local_file_read
python scripts/run_demo_scenario.py browser_snapshot
python scripts/export_audit.py --latest
python scripts/export_traces.py --latest
python scripts/check_database_connection.py        # tests DATABASE_URL without printing secrets
```

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Runs keep pausing for approval | Ollama not running or model not pulled; run `agentgate doctor` |
| `ModuleNotFoundError: fastapi` / `alembic` | Installed without extras; use `pip install -e ".[server]"` |
| `connection refused` on the DB | PostgreSQL not up, or wrong host/driver in `DATABASE_URL` |
| Playwright `NotImplementedError` (Windows) | Start with `python run.py` |
| Browser actions fail | `playwright install chromium` / `agentgate setup browser` |
| File read refused | Path outside `LOCAL_FILE_ROOT` and `ALLOWED_FILESYSTEM_PATHS` |
| Run state gone after restart | Live run state is in memory; only audit rows persist |
| Remote DB TLS errors | Set `DATABASE_SSL_MODE=require` (or `auto` for Neon) |
