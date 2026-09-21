# AgentGate

AgentGate is a **guardrail runtime for AI agents**. An LLM planner *proposes* actions (read a file, send a
Telegram message, create a calendar event, click a button, start a Stripe checkout). AgentGate evaluates each
one **before** it runs and decides: execute, block, ask a human, ask the user for missing or sensitive input,
or redact first. Every action leaves an audit record.

```
User prompt -> LLM planner -> ActionRequest -> Guardrail -> Decision router
            -> API / Browser executor -> ExecutionResult -> AuditEvent -> ActionTrace
```

## Two ways to run it

| Mode | Needs | Use it when |
|------|-------|-------------|
| **Local CLI** (`agentgate`) | Python 3.11+, LLM key, Ollama | You want guarded runs from a terminal, no server or database |
| **Server** (FastAPI + PostgreSQL) | Python 3.11+, PostgreSQL, Ollama, LLM key | You want the HTTP API, SSE streaming, OAuth, Stripe/Telegram webhooks, a browser demo |

Both share the same planner, guardrail, connectors, and approval semantics.

## Start here

| I want to... | Read |
|--------------|------|
| Install and run it | [Getting started](docs/getting-started.md) |
| Understand how it works | [Architecture](docs/architecture.md) |
| Know how decisions are made | [Guardrail policy](docs/guardrail-policy.md) |
| Call the API | [API reference](docs/api-reference.md) |
| Configure it | [Configuration](docs/configuration.md) |
| Connect Gmail / GitHub / Calendar / Telegram / Stripe | [Connectors](docs/connectors.md) |
| Deploy and operate it | [Deployment and operations](docs/deployment.md) |
| Assess the risks | [Security and threat model](docs/security.md) |
| See what is unfinished | [Known limitations](docs/limitations.md) |

Full index: [docs/index.md](docs/index.md).

## Server development quickstart

```bash
# Configure the planner, connectors, database password, and guardrail.
cp .env.example .env

# Start host Ollama with the configured detector model, then start the stack.
docker compose up -d --build
curl http://localhost:8000/api/v1/health

# Inspect or rerun migrations against the API container's database.
docker compose exec -T api alembic current
docker compose exec -T api alembic upgrade head
docker compose exec -T api python -m scripts.check_database_connection
```

Compose runs migrations after PostgreSQL becomes healthy. It uses
`postgres:5432/agentgate` by default; the host-oriented `DATABASE_URL` does not
replace that connection. Set `DOCKER_DATABASE_URL` only when the container and
its migrations should use another database. `DB_PASSWORD` configures the local
PostgreSQL service and fallback URL, but changing it does not rewrite credentials
inside an existing database volume.

The API container reaches host Ollama through `host.docker.internal`. Override
that endpoint with `DOCKER_OLLAMA_HOST`. On Linux, Ollama must listen on an
interface reachable from Docker. The example detector timeout is deliberately
long enough for CPU inference; measure a complete guarded request before lowering
`AGENTGATE_LLM_DETECTOR_TIMEOUT`.

After changing environment values used by the API, recreate the service because
a plain restart retains the container's old environment:

```bash
docker compose up -d --force-recreate api
```

For host development, install `.[dev,server,browser,stripe]`, point
`DATABASE_URL` at a reachable PostgreSQL instance, run `alembic upgrade head`,
and then start `uvicorn app.main:app --reload`. See
[deployment and operations](docs/deployment.md) for production checks and Ollama
troubleshooting.

## The five decisions

| Decision | Meaning |
|----------|---------|
| `ALLOW` | Execute now |
| `BLOCK` | Never execute |
| `NEED_APPROVAL` | Pause until a human approves or declines |
| `SANITIZE` | Sensitive content found or input missing; redact or ask the user to supply it |
| `ASK_USER` | Too little information; ask a clarifying question |

## What it can act on

| Target | Operations |
|--------|-----------|
| Local files | `read` (allowlisted directories only) |
| GitHub | `repo_metadata` |
| Gmail | `list_messages` (read-only) |
| Google Calendar | `list_events`, `create_event` |
| Telegram | `send_message`, `answer_callback_query`, `edit_message_reply_markup` |
| Stripe | checkout create/retrieve/expire, refund create/retrieve |
| Browser (Playwright) | open, snapshot, click, type, select, submit, scroll, screenshot |

## Quick check

```bash
pip install -e ".[dev,server,browser,stripe]"
pytest
```

The server imports Playwright at startup, so the `browser` extra is required even if you never use browser
actions (without it `app.main` and 7 test modules fail at import). Chromium itself
(`playwright install chromium`) is only needed for real browser runs.

## Contributing

See [docs/contributing.md](docs/contributing.md) and [docs/testing.md](docs/testing.md).
