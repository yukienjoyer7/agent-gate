# Telegram Integration

AgentGate supports Telegram as an inbound channel and outbound connector.
Telegram-originated prompts use the same reactive agent loop, guardrails,
approval flow, executor routing, and audit trail as `/api/v1/chat`.

## Bot Setup

1. Create a bot with BotFather in Telegram.
2. Copy the bot token into `.env` as `TELEGRAM_BOT_TOKEN`.
3. Generate a long random webhook secret and set it as `TELEGRAM_WEBHOOK_SECRET`.
4. Keep both values out of source control.

Required environment:

```bash
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
TELEGRAM_API_BASE=https://api.telegram.org
```

## Local Development

Telegram webhooks require a public HTTPS URL. For local development, run
AgentGate and expose it with a tunnel such as ngrok, Cloudflare Tunnel, or
another HTTPS reverse tunnel.

Run AgentGate:

```bash
uvicorn app.main:app --reload
```

Public webhook endpoint:

```text
https://<public-host>/api/v1/telegram/webhook
```

Register the webhook:

```bash
python scripts/set_telegram_webhook.py --url https://<public-host>/api/v1/telegram/webhook
```

The script sends Telegram the configured `TELEGRAM_WEBHOOK_SECRET` as the
webhook `secret_token`. It does not print the bot token or secret.

## Verification

After registration, send the bot a Telegram text message such as:

```text
Read sample.txt
```

Expected flow:

```text
Telegram webhook
-> Telegram channel service
-> shared start_agent_run()
-> AgentGate reactive agent loop
-> guardrail
-> approval / sanitize / execute
-> existing executor architecture
```

The webhook returns quickly after creating the run. The bot sends an accepted
message with the run id, then follows the run state without draining the SSE
event queue used by browser clients.

## Guardrails And Approvals

Outbound Telegram actions are normal AgentGate connector actions:

```json
{
  "action_type": "API_CALL",
  "target_system": "telegram",
  "domain": "productivity",
  "risk_hint": "external_send",
  "payload": {
    "action": "send_message",
    "chat_id": "123456",
    "text": "deployment completed"
  }
}
```

Because `external_send` is a side effect, the existing guardrail rules can
require approval before the action reaches:

```text
ExecutionRouter -> APIExecutor -> TelegramConnector -> Telegram Bot API
```

For Telegram-originated runs, approval prompts are sent back to the same chat
with inline Approve/Reject buttons. Button callbacks call the existing
`run_registry.respond(..., action="approve"|"decline")` path; there is no
separate Telegram approval state machine.

If a run reaches a sanitize or user-input pause, the bot sends a message telling
the user that additional input is required and the run must be resumed through
AgentGate.

## Production Notes

- Always set `TELEGRAM_WEBHOOK_SECRET`; an empty secret fails closed.
- Do not log or expose `TELEGRAM_BOT_TOKEN` or `TELEGRAM_WEBHOOK_SECRET`.
- Register webhooks during deployment, not during FastAPI startup.
- Use HTTPS only for the public webhook URL.
- Run a single process for the in-memory MVP deduplication and run registry, or
  add shared persistence before scaling horizontally.
