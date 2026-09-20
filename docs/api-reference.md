# API reference

Base path: `/api/v1`. FastAPI generates interactive docs at `/docs` and the OpenAPI spec at `/openapi.json`;
treat those as the source of truth for exact request and response bodies. CORS allows all origins
(no credentials). There is **no user authentication** (see [security](security.md)).

## Endpoints

| Tag | Method | Path | Purpose |
|-----|--------|------|---------|
| system | GET | `/` (no prefix) | Service name, env, version |
| health | GET | `/health` | `{"status":"ok"}` |
| chat | POST | `/chat/parse` | Turn a prompt into a plan of atomic steps (does not execute) |
| chat | POST | `/chat/execute` | Start a reactive run in the background; returns `run_id` |
| chat | POST | `/chat/execute/stream` | Same run, streamed as Server-Sent Events |
| chat | GET | `/chat/execute/{run_id}` | Live run state and per-step status |
| chat | POST | `/chat/execute/{run_id}/respond` | Answer a paused step |
| runs | GET | `/runs` | Runs with action counts and latest status |
| runs | GET | `/runs/{run_id}/actions` | Audit events for one run |
| actions | POST | `/actions/run` | Run a single guarded action proposal |
| actions | POST | `/actions/prototype/browser` | Browser prototype path |
| actions | GET | `/actions/{action_id}` | One action chain (404 if unknown) |
| audits | GET | `/audits?run_id=` | Audit events, optionally for one run |
| audits | GET | `/audits/latest` | Latest audit event, or `{}` |
| approvals | GET | `/approvals` | Audit records in `PENDING_APPROVAL` |
| benchmark | GET | `/benchmark` | `action_count`, `avg_total_ms`, `latest_status` |
| oauth | GET | `/oauth/status` | Connection status per provider |
| oauth | GET | `/oauth/{provider}/authorize` | Start OAuth: `github`, `gmail`, `calendar` |
| oauth | GET | `/oauth/{provider}/callback` | OAuth redirect target |
| stripe | POST | `/stripe/webhook` | Signed Stripe events |
| telegram | POST | `/telegram/webhook` | Inbound Telegram updates |

## Chat runs

### Start

`POST /chat/execute` with `{"prompt": "..."}` returns `{"run_id", "status", "prompt"}` immediately; the run
continues in the background.

### Inspect

`GET /chat/execute/{run_id}` returns the overall `RunStatus` and each step's `StepStatus`, so you can see which
step is waiting. `GET /runs/{run_id}/actions` returns the audit trail.

### Respond to a paused step

```bash
# approve or decline
curl -X POST localhost:8000/api/v1/chat/execute/$RUN/respond -H "Content-Type: application/json" \
     -d '{"step_index":0,"action":"approve"}'

# supply input for a sanitize step
curl -X POST localhost:8000/api/v1/chat/execute/$RUN/respond -H "Content-Type: application/json" \
     -d '{"step_index":0,"action":"input","fields":{"password":"..."}}'
```

`action` is `approve`, `decline`, or `input`. `input` requires `fields` or `text`; the other two must not send
them. The response is `{"run_id","step_index","action","status":"accepted","step_status"}`.

### Stream events

`POST /chat/execute/stream` keeps the connection open and emits SSE events:

| Event | Meaning |
|-------|---------|
| `planning`, `replanning` | Planner is producing (or revising) steps |
| `guardrail` | A verdict for a step |
| `step_status` | A step changed status (`index`, `status`) |
| `executing` | A step or browser batch started |
| `awaiting_approval` | Step paused for approve/decline |
| `awaiting_input` | Step paused for user input |
| `done`, `error` | Run finished or failed |

A heartbeat is sent every `SSE_HEARTBEAT_SEC`. Answer paused steps with `/respond`; the stream resumes.

## Audit queries

```bash
curl "localhost:8000/api/v1/audits?run_id=run_634a174c8449"
curl localhost:8000/api/v1/audits/latest
curl localhost:8000/api/v1/actions/act_0123456789ab
curl localhost:8000/api/v1/benchmark
```

## OAuth

`GET /oauth/{provider}/authorize` redirects to the provider; the provider returns to
`/oauth/{provider}/callback`. Providers: `github`, `gmail`, `calendar`. Redirect URIs come from
`GITHUB_OAUTH_REDIRECT_URI`, `GOOGLE_OAUTH_REDIRECT_URI`, `GOOGLE_CALENDAR_OAUTH_REDIRECT_URI` and must match
what you registered with the provider. See [connectors](connectors.md).

## Webhooks

| Endpoint | Authentication | Failure responses |
|----------|----------------|-------------------|
| `POST /stripe/webhook` | `Stripe-Signature` HMAC, checked with `STRIPE_WEBHOOK_SECRET`, tolerance `STRIPE_WEBHOOK_TOLERANCE_SEC` | 400 invalid signature or payload |
| `POST /telegram/webhook` | Header `X-Telegram-Bot-Api-Secret-Token` equal to `TELEGRAM_WEBHOOK_SECRET` | 403 invalid secret; 503 secret not configured |

## Notes

- `/approvals` lists audit records in `PENDING_APPROVAL`. Those rows come from the single-action path (`POST /actions/run`), which
  writes the row immediately. In interactive chat runs the approval wait happens **before** the audit row is written, so a
  waiting step appears in `GET /chat/execute/{run_id}` (status `waiting_approval`), not in `/approvals`. Decide chat steps with
  `POST /chat/execute/{run_id}/respond`.
- Endpoints that read audit data use `AUDIT_BACKEND` (`postgres` or `jsonl`).
- `avg_total_ms` in `/benchmark` averages `latency.total_ms` over all recorded actions.
