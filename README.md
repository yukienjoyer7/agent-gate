# AgentGate

Guarded agent execution platform (MVP). Agents propose actions; a guardrail layer
scores risk and routes them to auto-execution, human approval, or rejection — with
full audit trails and benchmarking of raw vs. guarded execution.

Architecture: **Modular Monolith** on **FastAPI / Python 3.11 / PostgreSQL**.
See the [Technical Foundation Document](./AgentGate%20Technical%20Foundation%20Document.md) for the full Sprint 0 design.

## Quickstart

```bash
# 1. Python env + deps
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Config
cp .env.example .env        # fill in values as needed

# 3. Services (Postgres + Redis)
docker compose up -d

# 4. Migrations
alembic upgrade head

# 5. Run the API
uvicorn app.main:app --reload
# -> http://localhost:8000/api/v1/health
```

## Tests

```bash
pytest
```

## Layout

| Path | Purpose |
|------|---------|
| `app/api/` | HTTP routes (versioned under `v1/`) |
| `app/domains/` | Domain logic: agent, guardrail, approval, audit, connector, browser, benchmark |
| `app/executors/` | API / browser execution + decision routing |
| `app/llm/` | LLM providers, tool registry, planner |
| `app/database/` | Session, base, Alembic migrations |
| `app/models/` | SQLAlchemy ORM models |
| `app/workers/` | Async workers (future Redis queue) |
| `deployment/` | Docker / Compose / nginx |

Status: **Sprint 1 — guarded local/browser demo path.**

## Sprint 1 demos

```bash
python scripts/run_demo_scenario.py local_file_read
python scripts/run_demo_scenario.py browser_snapshot
python scripts/export_audit.py --latest
python scripts/export_traces.py --latest
```

Audit events append to `artifacts/audit/events.jsonl` by default. Action traces
append to `artifacts/traces/actions.jsonl`. The browser path is a mock skeleton
until the Playwright executor is hardened.

## Interactive chat runs (reactive agent loop)

`POST /api/v1/chat/execute` now runs a **plan-then-react loop** instead of a
parse-once/run-straight-through pipeline:

```
plan -> (guardrail -> approve / sanitize / execute -> observe -> replan)*
```

- Every step is guardrail-checked **one at a time** before it runs.
- `NEED_APPROVAL` steps **pause** until you approve or decline them.
- Sensitive steps (empty `password`/`token`/`{{placeholder}}` payload values)
  get a **sanitize** status and pause until you type the value.
- On failure (e.g. a login form appears) or when the plan is exhausted (e.g.
  the calendar returned no events) the LLM **re-plans** the next step(s).

### Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/chat/execute` | Start a run in the background; returns `run_id` |
| `POST /api/v1/chat/execute/stream` | Same run, but streams every event as **SSE** (`planning`, `guardrail`, `step_status`, `executing`, `awaiting_approval`, `awaiting_input`, `replanning`, `done`, ...) |
| `POST /api/v1/chat/execute/{run_id}/respond` | Answer a paused step: `approve`, `decline`, or `input` (text / `fields`) for sanitize steps |
| `GET /api/v1/chat/execute/{run_id}` | Live run state: overall status + per-step status (see which step waits) |
| `GET /api/v1/runs/{run_id}/actions` | Poll the audit trail of a run |

Example SSE flow: start `POST /chat/execute/stream`, keep the connection open,
then `POST /chat/execute/{run_id}/respond` with
`{"step_index": 0, "action": "approve"}` (or
`{"action": "input", "fields": {"password": "..."}}`) — the stream resumes live.

### Guardrail with a dedicated model

The deterministic rules stay the first line of defence. Set
`GUARDRAIL_LLM_ENABLED=true` (and optionally `GUARDRAIL_MODEL`) in `.env` to
have a dedicated LLM judge review every non-BLOCK decision via tool-calling;
a rule-based BLOCK can never be overridden. The loop, planner and replanner
always use `LLM_MODEL` — only the guardrail uses its own model.

### LLM provider config

The LLM provider is configured entirely via env (see `.env.example`):

| Env var | Purpose |
|---------|---------|
| `LLM_TYPE` | API dialect: `openai` (OpenAI-compatible) or `anthropic` |
| `LLM_URL` | Full chat endpoint (e.g. `.../v1/chat/completions` or `.../v1/messages`) |
| `LLM_MODEL` | Model id (e.g. `openrouter/free`, `claude-...`) |
| `LLM_API_KEY` | API key (`Bearer` for openai, `x-api-key` for anthropic) |
| `LLM_TIMEOUT` / `LLM_MAX_TOKENS` | Request timeout / anthropic `max_tokens` |

The shared client (`app.llm.services.client`) adapts the canonical payload/
response between the two dialects automatically (system message, tools,
tool-call round-trips). `GUARDRAIL_MODEL` only overrides the guardrail's
model name — it always uses the same provider/type as the planner.

## Contributing

### Getting started

1. Fork/clone the repo and follow [Quickstart](#quickstart) to set up your environment.
2. Install dev tooling: `pip install -e ".[dev]"`.
3. Create a branch off `main` — never commit directly to `main`.

### Branch & commit conventions

- **Branches:** `<type>/<short-description>`, e.g. `feat/approval-queue`, `fix/audit-timestamp`.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) —
  `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`. Keep them small and focused.

### Where code goes

AgentGate is a **Modular Monolith** with strict domain boundaries. Put code in the
right place and keep domains decoupled:

- New HTTP endpoint → `app/api/v1/`, with logic delegated to a domain service.
- Business logic → the relevant `app/domains/<domain>/` package — never in the router.
- New connector → implement `BaseConnector.execute(action, payload)` from
  `app/domains/connector/base.py`.
- Cross-cutting config → `app/config/`; shared helpers → `app/utils/`.

### Before you push

Run the full local check — all must pass:

```bash
ruff check .          # lint
black --check .        # formatting (run `black .` to fix)
mypy app               # type checking
pytest                 # tests
```

- Add or update tests for any behavior change (`tests/unit`, `tests/integration`, `tests/e2e`).
- Update `alembic` migrations when models change: `alembic revision --autogenerate -m "<change>"`.
- **Never commit secrets.** Configuration lives in `.env*` (gitignored); only `.env.example` is tracked.

### Pull requests

- Keep PRs scoped to a single concern; link the related issue/sprint task.
- Describe the change, how you tested it, and any migration or config impact.
- Record significant architectural decisions as an ADR in `docs/decisions/`
  (see [`0001-architecture-foundation.md`](./docs/decisions/0001-architecture-foundation.md)).
- At least one review approval is required before merge; squash-merge into `main`.
