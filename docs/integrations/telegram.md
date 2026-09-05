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
    "recipient": "Rafi Ahmad",
    "text": "deployment completed"
  }
}
```

`recipient` is a human-readable reference, not a Bot API address. Before the
guardrail asks for approval, AgentGate resolves it against its persistent
Telegram contact registry and replaces it internally with a numeric `chat_id`.
The approval view shows the resolved display name and username when known; the
connector receives only the numeric chat ID and never guesses from a name.

The planner may use `chat_id` directly only when the user explicitly supplied
a numeric Telegram chat ID:

```json
{"action": "send_message", "chat_id": 123456789, "text": "deployment completed"}
```

An older plan that puts a name in `chat_id` is safely reinterpreted as a
recipient reference and still requires registry resolution.

## Recipient Registration And Resolution

Whenever the inbound webhook receives a valid Telegram `message.chat`, the
channel upserts that chat's ID, type, username, first/last name, display name,
and first/last-seen timestamps. This happens before any AgentGate run is
started, so a recipient pressing `/start` is enough to become addressable.
Duplicate webhook updates remain deduplicated and the database has a unique
constraint on `chat_id`.

Resolution is exact and case-insensitive where appropriate:

- a numeric chat ID supplied explicitly;
- an exact `@username` or username;
- an exact normalized display name.

AgentGate does not fuzzy-match names or select the first duplicate. An unknown
recipient enters the existing `ASK_USER` flow and no Telegram API request is
made. For an ambiguous display name, AgentGate lists the matching
display-name/username choices and asks for an exact `@username` or numeric chat
ID. The user must clarify before normal `external_send` approval begins.

Telegram's Bot API cannot globally search Telegram users or DM arbitrary names.
The bot can normally resolve a person only after an inbound interaction such as
`/start`, or from a group/channel identity already accessible to the bot. This
integration intentionally does not use MTProto, Telethon, or Pyrogram.

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

## Manual End-to-End Check

1. Rafi opens the AgentGate bot and presses `/start`.
2. Confirm the webhook accepts the update; this records Rafi's chat identity.
3. In AgentGate Web Chat, enter `kirim pesan telegram "halo" ke Rafi Ahmad`.
4. Confirm the plan has `recipient: "Rafi Ahmad"`, `target_system: telegram`,
   and `risk_hint: external_send`.
5. Confirm AgentGate resolves the recipient and displays `Rafi Ahmad
   (@rafiahmad)` (when that username is registered) in the approval context.
6. Before approval, verify no `sendMessage` request has been made.
7. Approve the action. The connector then sends the already-resolved numeric
   chat ID, Telegram returns HTTP 200, Rafi receives `halo`, and the audit trail
   records the reference, resolved identity, NEED_APPROVAL/approval transition,
   and execution result.

## Production Notes

- Always set `TELEGRAM_WEBHOOK_SECRET`; an empty secret fails closed.
- Do not log or expose `TELEGRAM_BOT_TOKEN` or `TELEGRAM_WEBHOOK_SECRET`.
- Telegram HTTP request logs are deliberately credential-free: they record the
  method and status (for example `sendMessage -> HTTP 200`) and redact any
  token-bearing Bot API URL emitted by `httpx`.
- Register webhooks during deployment, not during FastAPI startup.
- Use HTTPS only for the public webhook URL.
- Run a single process for the in-memory MVP deduplication and run registry, or
  add shared persistence before scaling horizontally.
