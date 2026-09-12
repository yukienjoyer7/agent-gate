# Stripe integration

AgentGate supports a deliberately small Stripe API surface. Financial writes
pass through the normal action request, guardrail, approval, executor, and audit
pipeline. Customers enter payment details only on Stripe-hosted Checkout.

## Supported actions

| Action | Required payload | Behavior |
| --- | --- | --- |
| `create_checkout_session` | `catalog_key`, optional `quantity` and `customer_email` | Creates a one-time hosted Checkout Session |
| `retrieve_checkout_session` | `session_id` | Retrieves safe checkout status fields |
| `expire_checkout_session` | `session_id` | Expires an open session |
| `create_refund` | `payment_intent_id`, optional `amount` and `reason` | Creates a full or partial refund |
| `retrieve_refund` | `refund_id` | Retrieves safe refund status fields |

There is no generic Stripe request action. Prices, currencies, redirect URLs,
payouts, transfers, and arbitrary Stripe API paths cannot be supplied by the
planner.

## Configure Stripe

Use Stripe sandbox/test mode first. Store real values in an ignored `.env` or a
deployment secret manager, never in a tracked environment template.

```env
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_SUCCESS_URL=http://localhost:8000/payment/success
STRIPE_CANCEL_URL=http://localhost:8000/payment/cancel
STRIPE_PRICE_MAP={"workshop_ticket":"price_...","pro_plan":"price_..."}
STRIPE_MAX_QUANTITY=20
STRIPE_WEBHOOK_TOLERANCE_SEC=300
```

`STRIPE_PRICE_MAP` is the server-side allowlist. A prompt can select
`workshop_ticket`, but only the server can resolve it to a real Stripe Price ID.
Use a restricted key with only the permissions needed for Checkout Sessions and
refunds when possible.

Apply the database migration before receiving Stripe traffic:

```bash
uv run alembic upgrade head
```

The Stripe reconciliation tables are created by migration `0004`. If Checkout
creation succeeds but the action reports `Stripe session was created but local
reconciliation failed`, check the API log for a database error and run the
migration against the same `DATABASE_URL` used by the API. Do not retry a live
checkout until the migration has completed; retries within the same AgentGate
run use a stable Stripe idempotency key and can safely recover the existing
Checkout Session after persistence is restored.

## Local webhook setup

Install and authenticate the Stripe CLI, then forward sandbox events:

```bash
stripe listen --forward-to localhost:8000/api/v1/stripe/webhook
```

Copy the CLI-provided `whsec_...` value into `STRIPE_WEBHOOK_SECRET`. The secret
for CLI forwarding is different from the secret assigned to a Dashboard webhook
endpoint.

Recommended event subscriptions:

- `checkout.session.completed`
- `checkout.session.async_payment_succeeded`
- `checkout.session.async_payment_failed`
- `checkout.session.expired`
- `payment_intent.succeeded`
- `payment_intent.payment_failed`
- `charge.refunded`

The endpoint verifies `Stripe-Signature` against the untouched request body and
persists each Stripe event ID once. Duplicate deliveries return a successful
response without repeating state changes.

## Planner examples

Create a hosted checkout:

```json
{
  "action_type": "API_CALL",
  "target_system": "stripe",
  "domain": "booking",
  "risk_hint": "payment",
  "target": "workshop_ticket",
  "payload": {
    "action": "create_checkout_session",
    "catalog_key": "workshop_ticket",
    "quantity": 1,
    "customer_email": "buyer@example.com"
  }
}
```

Create a partial refund. `amount` is an integer in the currency's minor unit:

```json
{
  "action_type": "API_CALL",
  "target_system": "stripe",
  "domain": "booking",
  "risk_hint": "refund",
  "target": "pi_...",
  "payload": {
    "action": "create_refund",
    "payment_intent_id": "pi_...",
    "amount": 1250,
    "reason": "requested_by_customer"
  }
}
```

AgentGate overwrites an incorrect Stripe domain or risk hint at the trusted
`ActionRequest` boundary. Checkout creation, expiration, refunds, and the
critical booking domain therefore cannot bypass approval by claiming a low-risk
planner value.

## Stored data

`stripe_payments` contains only Stripe object IDs, AgentGate run/action IDs,
status, amount, currency, and timestamps. `stripe_webhook_events` contains only
event IDs and processing metadata for deduplication. Full webhook payloads, card
details, customer email addresses, client secrets, and API credentials are not
stored in these tables.

For production, set the Dashboard webhook endpoint to
`https://your-host/api/v1/stripe/webhook`, match its API version to the pinned
Stripe SDK release, use HTTPS, rotate keys, and monitor failed webhook delivery.
