# Testing

About 350 test functions across unit, integration, and vendored-guardrail suites.

## Run

```bash
pip install -e ".[dev,server,browser,stripe]"
pytest                          # everything
pytest tests/unit               # fast, no external services
pytest tests/integration
pytest tests/upstream_guardrail # tests of the embedded guardrail engine
pytest -k calendar -x           # by keyword, stop at first failure
```

Lint and types:

```bash
ruff check .
black --check .
mypy app
```

## Layout

| Path | Covers |
|------|--------|
| `tests/test_health.py` | Health and root endpoints |
| `tests/unit/` | Agent loop, decision flows, guardrail (hybrid, sensitive, diagnostics), schemas, LLM client and parser, run registry/service, connectors (GitHub, Gmail, Calendar, Telegram, Stripe), OAuth, audit repository, DB session, CLI, credentials, local browser and payments |
| `tests/integration/` | End-to-end flows: local file and browser demo paths, Sprint 2 endpoints and traces, chat agent endpoints, calendar flow, Telegram and Stripe webhooks, agentgate guardrail |
| `tests/upstream_guardrail/` | Vendored engine: text scanning, fail-closed detectors, decision concurrency; uses `fake_llm.py` and `fake_audit.py` |
| `tests/e2e/`, `tests/fixtures/` | Empty placeholders |

## Isolation

`tests/conftest.py` applies an autouse fixture that makes tests independent of your machine:

- `GUARDRAIL_BACKEND=legacy` and `GUARDRAIL_LLM_ENABLED=false` (no Ollama or LLM needed)
- `AUDIT_BACKEND=jsonl` with audit files in `tmp_path`
- `LLM_TYPE=openai`, `ATOMIC_BROWSER_AUDIT=false`
- `get_settings.cache_clear()` before and after

Tests that exercise the embedded engine select `agentgate` explicitly and fake only the Ollama transport.

## Writing tests

- Call `get_settings.cache_clear()` after changing environment variables with `monkeypatch`.
- Use `tmp_path` for audit, trace, and `LOCAL_FILE_ROOT` so tests never touch real data.
- Fake external systems at the `httpx` client boundary; connectors accept an injected `httpx.AsyncClient`.
- Add a test for every behaviour change; put connector tests next to the existing `test_<name>_connector.py`.
- For new guardrail rules, cover BLOCK, NEED_APPROVAL, SANITIZE, and ALLOW paths and the fail-closed case.

## What is not tested here

Live provider calls (Gmail, GitHub, Calendar, Stripe, Telegram), a real PostgreSQL migration run in CI, and a
real Ollama detector are not part of the default suite. Verify those manually with the setup steps in
[getting-started](getting-started.md) and [connectors](connectors.md).
