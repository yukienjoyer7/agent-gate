# Configuration

Server settings load from environment variables and `.env` through `app/config/settings.py`
(pydantic-settings). `APP_ENV` selects a subclass with different defaults. Templates: `.env.example`,
`.env.development`, `.env.staging`. The **CLI does not read `.env`**; it uses `config.toml` and the credential
store ([cli.md](cli.md)).

Values below are the defaults in code. Where a template overrides one, it is noted.

## Core

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_ENV` | `development` | `development`, `staging`, `production` |
| `DEBUG` | `False` | Debug mode (True in development profile) |
| `LOG_LEVEL` | `INFO` | `DEBUG`..`ERROR` (DEBUG in development profile). Structured JSON logs |

## Database and audit

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `postgresql+psycopg://agentgate:agentgate@localhost:5432/agentgate` | Must start with `postgresql` or `sqlite`. `.env.example` uses `+asyncpg` |
| `DATABASE_POOL_SIZE` | 10 (1-100) | Pool size |
| `DATABASE_MAX_OVERFLOW` | 20 | Extra connections |
| `DATABASE_SSL_MODE` | `auto` | `auto` verifies TLS for Neon / `sslmode=require` URLs; `require`; `disable` (asyncpg driver only) |
| `AUDIT_BACKEND` | `postgres` | `postgres` or `jsonl`. `.env.development` sets `jsonl` |
| `ATOMIC_BROWSER_AUDIT` | `False` | One audit row per browser step instead of one per batch |
| `AUDIT_LOG_PATH` | `artifacts/audit/events.jsonl` | JSONL audit file |
| `TRACE_LOG_PATH` | `artifacts/traces/actions.jsonl` | Action traces |
| `GUARDRAIL_AUDIT_PATH` | `artifacts/audit/guardrail.jsonl` | Guardrail evaluation journal |
| `AUDIT_RETENTION_DAYS` | 7 | Staging 30, production 365 |
| `TRACE_RETENTION_DAYS` | 7 | Staging 14, production 90 |
| `SCREENSHOT_RETENTION_DAYS` | 7 | Production 30 |
| `DATA_DIR` | `./data` | Local data (screenshots under `data/browser/screenshots/`) |

Retention values are configuration only. No cleanup job was found in the code reviewed; see [limitations](limitations.md).

## LLM planner

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_TYPE` | `openai` | `openai` (compatible), `anthropic`, or `gemini` |
| `LLM_URL` | `https://openrouter.ai/api/v1/chat/completions` | Full chat endpoint |
| `LLM_MODEL` | `openrouter/free` | Model id |
| `LLM_API_KEY` | empty | `Bearer` (openai) or `x-api-key` (anthropic) |
| `LLM_TIMEOUT` | 60 | Seconds |
| `LLM_MAX_TOKENS` | 4096 (256-128000) | Response cap |
| `LLM_TOOLS_ENABLED` | True | Tool calling |
| `LLM_MAX_TOOL_ITERATIONS` | 5 (1-10) | Tool-call rounds |
| `LLM_PLUGINS` | empty | Provider-specific request plugins; leave empty if unsupported |

## Planner validation

Each takes a comma-separated list. Anything outside these lists is rejected or blocked.

| Variable | Default |
|----------|---------|
| `ALLOWED_ACTION_TYPES` | `BROWSER_OPEN, BROWSER_CLICK, BROWSER_TYPE, BROWSER_SCROLL, BROWSER_SCREENSHOT, BROWSER_SUBMIT, BROWSER_SELECT, API_CALL, FILE_READ` |
| `ALLOWED_TARGET_SYSTEMS` | `browser, calendar, gmail, github, local_file, stripe, telegram` |
| `ALLOWED_DOMAINS` | `browser, productivity, code_protection, booking, filesystem` |
| `ALLOWED_RISK_HINTS` | `unknown, external_send, file_read, destructive, unauthorized, data_exfiltration, payment, refund, bulk_action` |
| `INTERACTIVE_BROWSER_ACTIONS` | `BROWSER_CLICK, TYPE, SCROLL, SCREENSHOT, SUBMIT, SELECT` |
| `DOMAIN_BY_TARGET_SYSTEM` | calendar/gmail/telegram -> productivity; github -> code_protection; local_file -> filesystem; stripe -> booking; browser -> browser |
| `DEFAULT_DOMAIN` | `productivity` |

`DOMAIN_BY_TARGET_SYSTEM` stops the planner from lowering risk by omitting or misreporting `domain`.

## Guardrail

| Variable | Default | Purpose |
|----------|---------|---------|
| `GUARDRAIL_BACKEND` | `agentgate` | `agentgate` (embedded engine) or `legacy` (explicit rollback, no auto fallback) |
| `OLLAMA_HOST` | `http://localhost:11434` | Detector endpoint. Remote hosts require HTTPS; `host.docker.internal` is allowed over HTTP |
| `AGENTGATE_LLM_DETECTOR_MODEL` | `qwen2.5:7b` | Detector model |
| `AGENTGATE_LLM_DETECTOR_TIMEOUT` | 30 | Seconds per request, no retries |
| `AGENTGATE_DETECTOR_ARCHITECTURE` | `six` | `six` or experimental `unified` |
| `GUARDRAIL_BLOCK_HINTS` | `destructive, unauthorized, data_exfiltration` | Hints that map to `BLOCK` |
| `GUARDRAIL_NEED_APPROVAL_HINTS` | `external_send, payment, bulk_action, refund` | Hints that map to `NEED_APPROVAL` |
| `GUARDRAIL_ASK_USER_HINTS` | `ambiguous_target, missing_target, clarification_needed` | Hints that map to `ASK_USER` |
| `GUARDRAIL_LLM_ENABLED` | `True` in code, `False` in `.env.example` | **Legacy backend only:** second-opinion LLM judge |
| `GUARDRAIL_MODEL` | empty | Legacy judge model; empty falls back to `LLM_MODEL` |

Run Ollama with `OLLAMA_NUM_PARALLEL=6` so the six detectors can overlap. The CLI reads
`OLLAMA_HOST`, `AGENTGATE_LLM_DETECTOR_MODEL`, `AGENTGATE_LLM_DETECTOR_TIMEOUT`,
`AGENTGATE_DETECTOR_ARCHITECTURE` from the shell only.

## Agent loop

| Variable | Default | Purpose |
|----------|---------|---------|
| `AGENT_MAX_STEPS` | 12 (1-100) | Steps per run, including replanned steps |
| `AGENT_MAX_REPLAN` | 4 (0-20) | Replan calls |
| `AGENT_WAIT_RESPONSE_TIMEOUT_SEC` | 600 | Wait for approval/input |
| `AGENT_RUN_TIMEOUT_SEC` | 900 | Whole-run timeout |
| `SSE_HEARTBEAT_SEC` | 15 | SSE keep-alive |
| `RUN_REGISTRY_MAX_SESSIONS` | 500 | In-memory run cap |

## Filesystem and browser

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOCAL_FILE_ROOT` | `demo_data` | Root for file reads |
| `ALLOWED_FILESYSTEM_PATHS` | `/tmp/agentgate` | Extra **absolute** allowed dirs (comma-separated or JSON list) |
| `PLAYWRIGHT_HEADLESS` | True | `.env.development` sets False |
| `PLAYWRIGHT_MAX_ELEMENTS` | 50 (10-500) | Elements per snapshot |
| `BROWSER_MAX_CONCURRENT_SESSIONS` | 10 | Staging 25, production 50 |
| `BROWSER_WAIT_UNTIL` | `domcontentloaded` | `commit`, `domcontentloaded`, `load`, `networkidle` |
| `BROWSER_TIMEOUT_MS` | 15000 (1000-60000) | Navigation timeout |
| `BROWSER_SETTLE_MS` | 2000 (0-10000) | Wait after load |
| `BROWSER_USER_AGENT` | empty | Empty uses the built-in Chrome profile |

## Connectors

| Group | Variables |
|-------|-----------|
| GitHub | `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`, `GITHUB_OAUTH_REDIRECT_URI`; fallback token `GITHUB_TOKEN` |
| Google | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI` (Gmail), `GOOGLE_CALENDAR_OAUTH_REDIRECT_URI`; fallback tokens `GMAIL_ACCESS_TOKEN`, `GOOGLE_CALENDAR_ACCESS_TOKEN` |
| Calendar | `CALENDAR_DEFAULT_TIMEZONE` (default `Asia/Jakarta`) |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_API_BASE` |
| Stripe | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_SUCCESS_URL`, `STRIPE_CANCEL_URL`, `STRIPE_PRICE_MAP` (JSON), `STRIPE_MAX_QUANTITY` (20), `STRIPE_WEBHOOK_TOLERANCE_SEC` (300) |
| Other | `XENDIT_API_KEY` is defined in settings; no code path using it was found |

Static tokens (`GITHUB_TOKEN`, etc.) are a fallback used when no OAuth token is stored.

## Environment profiles

| | development | staging | production |
|-|-------------|---------|------------|
| `DEBUG` / `LOG_LEVEL` | True / DEBUG | False / INFO | False / INFO |
| Audit / trace retention (days) | 7 / 7 | 30 / 14 | 365 / 90 |
| Screenshot retention (days) | 7 | 7 | 30 |
| Max browser sessions | 10 | 25 | 50 |

Profile classes set only the values above. Other differences come from the `.env.*` templates
(for example `PLAYWRIGHT_HEADLESS` and `AUDIT_BACKEND`).

Never commit real secrets. `.env.development` and `.env.staging` are tracked (only `.env.production` is ignored
via the commented rule), so keep real values out of them; use a secret manager in production (ADR 0001).
