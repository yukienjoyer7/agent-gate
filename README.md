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
