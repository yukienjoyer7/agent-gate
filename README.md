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
pip install -e ".[dev,server]"
pytest
```

## Contributing

See [docs/contributing.md](docs/contributing.md) and [docs/testing.md](docs/testing.md).
