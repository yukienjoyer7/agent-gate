# Connectors

Every connector implements `BaseConnector.execute(action, payload) -> ExecutionResult`
(`app/domains/connector/base.py`), returns normalized `ConnectorError` codes on failure, and is reached only
after a guardrail verdict. The `payload.action` field selects the operation; `target_system` selects the connector.

## Summary

| Connector | `target_system` | Operations | Auth | Guardrail floor |
|-----------|-----------------|-----------|------|-----------------|
| Local files | `local_file` | `read` | none (path allowlist) | none |
| GitHub | `github` | `repo_metadata` | OAuth (`repo` scope) or `GITHUB_TOKEN` | none; domain `code_protection` is HIGH |
| Gmail | `gmail` | `list_messages` | OAuth `gmail.readonly` or `GMAIL_ACCESS_TOKEN` | none |
| Calendar | `calendar` | `list_events`, `create_event` | OAuth `calendar` (full) or token | create: `external_send` |
| Telegram | `telegram` | `send_message`, `answer_callback_query`, `edit_message_reply_markup` | Bot token | `external_send` |
| Stripe | `stripe` | create/retrieve/expire checkout session, create/retrieve refund | Secret key | `payment` / `refund` |
| Browser | `browser` | open, snapshot, click, type, select, submit, scroll, screenshot | none | submit needs approval |

Errors map to `AUTH`, `PERMISSION`, `RATE_LIMIT`, `TIMEOUT`, `VALIDATION`, `UNAVAILABLE`, `NOT_FOUND`,
`UNKNOWN`. Timeouts and unavailable services are marked `retryable`.

## Local files

- Reads only `read`, returning up to 500 characters as `content_preview`.
- The path is resolved (symlinks and `..` collapsed) and must sit inside `LOCAL_FILE_ROOT` or one of
  `ALLOWED_FILESYSTEM_PATHS`. Otherwise the result is `FAILED` with a validation error.
- Payload: `{"action":"read","path":"sample.txt"}`.
- Setup: put files under `LOCAL_FILE_ROOT` (default `demo_data/`).

## GitHub

- Operation `repo_metadata`, payload `{"action":"repo_metadata","owner":"...","repo":"..."}` (or a combined repo reference).
- Returns id, full name, privacy, default branch, URL, description, stars, forks, open issues.
- Auth options:
  1. **OAuth (server):** register a GitHub OAuth app; set `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`,
     `GITHUB_OAUTH_REDIRECT_URI` (default `http://localhost:8000/api/v1/oauth/github/callback`), open
     `/api/v1/oauth/github/authorize`. Scope requested: `repo`.
  2. **Token:** `GITHUB_TOKEN` (server) or `agentgate connect github` (CLI).
- Without a token, public repositories can still be read subject to GitHub's unauthenticated rate limits.

## Gmail

- Operation `list_messages`, payload `{"action":"list_messages","query":"...","max_results":10}`.
- Returns message IDs and a size estimate. **Read-only**: it cannot send or modify mail.
- Setup (Google Cloud Console):
  1. Create a project and enable the **Gmail API**.
  2. Configure the OAuth consent screen (add yourself as a test user while unverified).
  3. Create an OAuth client; add the redirect URI `http://localhost:8000/api/v1/oauth/gmail/callback`.
  4. Set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`.
  5. Open `/api/v1/oauth/gmail/authorize` (server) or run `agentgate connect gmail` (CLI, Desktop OAuth).
- Scope: `https://www.googleapis.com/auth/gmail.readonly`. Offline access with `prompt=consent` is requested so a
  refresh token is issued.

## Google Calendar

- Operations: `list_events` (optional `max_results`, default 10) and `create_event`.
  `create_event` payload: `summary`, `start`, `end` (required), optional `timezone`, `description`, `location`,
  `calendar_id` (default `primary`). The payload is validated first; `summary` and `description` can be redacted.
- Timezone default: `CALENDAR_DEFAULT_TIMEZONE` (`Asia/Jakarta`).
- Setup: enable the **Google Calendar API** on the same OAuth client and **also register**
  `GOOGLE_CALENDAR_OAUTH_REDIRECT_URI` (`.../oauth/calendar/callback`) as an authorized redirect URI.
  Then open `/api/v1/oauth/calendar/authorize` or run `agentgate connect calendar`.
- Scope: `https://www.googleapis.com/auth/calendar` (full read/write). Grant only to accounts you trust the agent with.

## Telegram

- Operations: send a message, answer a callback query, edit a reply markup.
- `recipient` is a human-readable name resolved against the contact registry (`telegram_contacts`) into a
  numeric `chat_id` **before** approval; the connector never guesses from a name. A numeric `chat_id` is used
  directly only if the user gave one.
- Outbound sends work from both the CLI and the server. **Inbound** (the webhook that starts runs and receives
  approval callbacks) is server-only; ADR 0002 keeps it out of the local CLI release.
- Inbound: the webhook (`/telegram/webhook`) starts runs and receives approval button callbacks. It requires
  `TELEGRAM_WEBHOOK_SECRET`; register it with `scripts/set_telegram_webhook.py`.
- Full guide: [integrations/telegram.md](integrations/telegram.md).

## Stripe

- Small, deliberate surface: hosted Checkout Sessions and refunds only. Prices come from the server-side
  allowlist `STRIPE_PRICE_MAP`; the planner selects a catalog key, never a raw price ID or amount.
- Approval is required for financial writes. Use **test mode** keys first.
- Webhook `/stripe/webhook` verifies the `Stripe-Signature` HMAC and deduplicates by event ID (server).
- **CLI:** `agentgate connect stripe` takes a user-supplied test-mode key through hidden input (not Stripe Connect
  OAuth). Checkout opens in the user's browser so card details never touch AgentGate. There is no webhook locally:
  `agentgate payments sync` retrieves tracked sessions/refunds and stores the provider status with a checked-at
  time. A redirect is never proof of payment, and nothing updates while the CLI is closed. Configure real
  `stripe_success_url` / `stripe_cancel_url`; there are no local landing pages.
- Retries reuse the persisted operation and idempotency key; unknown outcomes are reconciled before another
  financial attempt is allowed.
- Full guide: [integrations/stripe.md](integrations/stripe.md).

## Browser

- Playwright (Chromium). Requires `playwright install chromium` (server) or `agentgate setup browser` (CLI).
- Element IDs are short and snapshot-scoped; the selector map never leaves the server.
- Submitting a form is `NEED_APPROVAL` by policy, including submit-capable clicks.
- Tuning: `PLAYWRIGHT_*`, `BROWSER_*` in [configuration](configuration.md).

## Adding a connector

1. Create `app/domains/connector/<name>/` with a class extending `BaseConnector`; return `ExecutionResult` and
   `ConnectorError` on failure. Never log tokens.
2. Register it in `APIExecutor.connectors` (`app/executors/api_executor.py`).
3. Add each operation to the host tool table `_TOOLS` in `app/domains/guardrail/decision/agentgate.py` with its
   trusted risk hints and the content fields that may be redacted. **Unregistered operations are blocked.**
4. Add the system to `ALLOWED_TARGET_SYSTEMS` and `DOMAIN_BY_TARGET_SYSTEM` in settings.
5. Add planner guidance for the new operations, config variables to `.env.example`, unit tests, and a section here.
