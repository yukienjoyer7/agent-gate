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

Status: **Sprint 0 — repo foundation.** Domain logic lands in Sprint 1.

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
