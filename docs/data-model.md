# Data model

## Server (PostgreSQL) tables

Created by Alembic migrations `0001`-`0004`.

### `audit_logs` (migration 0001)

One immutable row per action. See [audit-design](audit-design.md).

| Column | Type | Notes |
|--------|------|-------|
| `audit_id` | varchar(32) PK | |
| `run_id` | varchar(32), indexed | |
| `action_id` | varchar(32), unique | |
| `request_json`, `decision_json`, `execution_json` | JSONB | Request excludes `payload` |
| `execution_status` | varchar(32) | |
| `error_type` | varchar(64), null | |
| `policy_version`, `detector_version` | varchar(32) | Default `policy-0.1`, `detector-0.1` |
| `latency` | JSONB | Default `{}` |
| `created_at` | timestamptz | Default `now()` |

Trigger `trg_audit_logs_immutable` raises an exception on any UPDATE or DELETE.

### `oauth_tokens` (0002)

| Column | Type | Notes |
|--------|------|-------|
| `provider` | varchar(32) PK | `github`, `gmail`, `calendar` |
| `access_token` | text | Stored as plain text |
| `refresh_token` | text, null | Stored as plain text |
| `expires_at` | timestamptz, null | |
| `scope` | text, null | |
| `updated_at` | timestamptz | |

### `telegram_contacts` (0003)

`id` PK; `chat_id` (bigint, unique); `chat_type`; `username`, `first_name`, `last_name`, `display_name`
(indexed where noted); `is_active`; `first_seen_at`; `last_seen_at`. Filled from inbound messages and used to
resolve human recipient names to numeric chat IDs.

### `stripe_payments` and `stripe_webhook_events` (0004)

| Table | Columns |
|-------|---------|
| `stripe_payments` | `id`, `stripe_session_id`, `payment_intent_id`, `run_id`, `action_id`, `status`, `amount` (bigint), `currency`, `created_at`, `updated_at` |
| `stripe_webhook_events` | `id`, `stripe_event_id` (unique, gives idempotency), `event_type` (indexed), `livemode`, `processed_at` |

## Local CLI stores

| Store | Location | Content |
|-------|----------|---------|
| `state.sqlite3` | `~/.local/share/agentgate/` (or `AGENTGATE_DATA_DIR`) | Run history, safe audit/traces, OAuth metadata, payment status, execution intents. Append-only audit via SQLite triggers |
| `guardrail.jsonl` | Same directory | One record per guardrail evaluation (the evaluation journal, ADR 0003) |
| `config.toml` | `~/.config/agentgate/` (or `AGENTGATE_CONFIG_DIR`) | Non-secret settings |
| OS keychain | | Tokens and keys (or environment / session, by credential mode) |

### Execution intents (CLI)

Per ADR 0002, an **intent journal** records an approved operation *before* its external effect and its outcome
afterward. If the intent cannot be durably recorded, the write is not performed. Financial operations also persist
a stable operation identity and idempotency data first. The journal holds no customer email, passwords, tokens, or
raw browser input. An ambiguous remote outcome stays `unknown` until reconciled (`agentgate payments sync`).
Crash recovery marks abandoned runs interrupted and **never replays** payments, messages, or calendar writes.

## JSONL files (server, optional)

| File | Written when |
|------|--------------|
| `artifacts/audit/events.jsonl` | `AUDIT_BACKEND=jsonl` |
| `artifacts/traces/actions.jsonl` | Every guarded action |
| `artifacts/audit/guardrail.jsonl` | Every guardrail evaluation (server default path). **Server deployments must persist this file** (ADR 0003) |

`/artifacts/audit/` and `/artifacts/traces/` are gitignored.

## What is never stored

`ActionRequest.payload` is excluded from serialized audit records. Sensitive entities are recorded as kinds,
not values. Guardrail journal entries pass through a sanitizer. See [security](security.md) for the exceptions
(OAuth tokens in the database).
