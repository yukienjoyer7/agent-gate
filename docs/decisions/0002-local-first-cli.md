# ADR 0002: Local-first CLI runtime

- Status: Accepted
- Date: 2026-09-13
- Scope: Local foreground CLI. See [the implemented command guide](../cli.md).
- Relationship: Keeps ADR 0001's modular monolith and domain boundaries. Introduces
  a local CLI as the first packaged runtime, with the HTTP server as a later adapter.

## Decision

Package AgentGate as an installable Python command that runs the existing agent
engine directly in the terminal process. Ordinary runs do not start FastAPI,
require Docker, connect to PostgreSQL or Redis, or call a localhost application API.

The first release targets one user, one profile, and one foreground run at a time.
Local-first means execution, approvals, credentials, and history are owned by the
user's machine. It does not mean offline: configured LLMs and external connectors
still make outbound network requests. Prompts and observations sent to an LLM leave
the machine, so users must be told what each integration shares.

```text
Terminal user
    |
CLI adapter: arguments, rendering, approval and secret input
    |
Agent application service: submit / events / respond / cancel / history
    |
LocalRuntime: constructs dependencies and owns their lifecycle
    |
Existing core: plan -> guardrail -> approve or clarify -> execute -> observe
                   ^                                       |
                   +--------------- replan ----------------+
    |
    +-- Connectors: local files, browser, GitHub, Gmail, Calendar, Stripe
    +-- SQLite: run history, safe events, audit, execution intents, payment state
    +-- OS keychain: API keys and OAuth access/refresh tokens
    +-- User config: non-secret settings and enabled capabilities

Later: an HTTP adapter calls the same application service with a ServerRuntime.
```

## What can be reused, and what must change

The existing `start_agent_run` service already launches an asynchronous run without
HTTP. `RunSession.events` provides events, and `RunRegistry.respond` resolves
approval/input waiters. Keep these behaviors and the planner, guardrail, connector
contracts, and run-scoped browser sessions. The CLI is another interaction adapter,
not a second agent implementation.

The implementation addressed these original packaging seams:

- `app/database/session.py` constructs a PostgreSQL engine at import time and
  normalizes URLs for asyncpg. A SQLite URL alone will not make it a local runtime.
- The audit repository selector eagerly imports its database implementation.
  Selecting JSONL does not remove database dependencies from OAuth or Stripe.
- OAuth repositories depend on global `SessionLocal` and currently store token
  values in database rows. Local credentials need a separate secret store.
- `app/executors/api_executor.py` eagerly imports and constructs every connector.
  Optional integrations need lazy registration and injected dependencies.
- Settings mix core behavior with server URLs and infrastructure. Local runs must
  not require server settings, or use nonexistent localhost checkout landing pages.
- Migration `0001` contains PostgreSQL JSONB and trigger SQL. Preserve the existing
  migration history; introduce separately tested local schema migrations.
- `pyproject.toml` has no console entry point and includes server dependencies in
  the base installation. Installed execution must also stop relying on repository
  paths for assets, data, or config.

## Application boundary and composition

Introduce a small application facade exposing run submission, event observation,
responses, cancellation, and historical queries. Requests and safe event DTOs must
not contain FastAPI objects or terminal rendering concerns. Responses identify the
run and exact pending action; changed actions require new approval.

`LocalRuntime` injects validated settings, the planner, guardrail, executor/connector
registry, storage, credentials, and interaction/event services. It initializes and
closes resources explicitly. Importing core modules must not open databases,
launch browsers, validate unrelated credentials, or import optional server SDKs.

Use narrow repository contracts for run history, audit, execution intents, OAuth
metadata, and payments. Keep the existing `BaseConnector` execution contract.
Do not introduce a generic plugin platform or remote transport in the first version.

Implemented additions, alongside the existing domain packages:

- `app/cli/`: entry point, commands, terminal interaction, safe output renderer.
- `app/runtime/`: dependency contracts and local composition/lifecycle.
- `app/storage/local/`: SQLite repositories and versioned local migrations.
- `app/credentials/`: OS keychain and explicit environment/session providers.

Existing HTTP routes stay intact during extraction. A future `ServerRuntime` can
select PostgreSQL and server-managed credentials. Authentication, ownership,
durable workers, event fan-out, and reconnect semantics remain server-specific
work, not something dependency injection solves automatically.

## Foreground execution lifecycle

1. Parse arguments before initializing the runtime. `--help` needs no credentials,
   database, browser, or network connection.
2. Resolve trusted configuration and enabled capabilities. Initialize local schema
   and acquire a per-profile execution lock; reject a second active run in v1.
3. Create and persist the run ID. Start the engine and consume its events within
   the same asynchronous application lifetime.
4. Display safe progress. For approvals, show the exact action, target, consequence,
   and financial details when relevant. For clarification, collect requested fields.
   Use hidden input for secrets and pass responses to the engine's waiters.
5. Persist safe events and action outcomes. Await the engine task as well as the
   event consumer so exceptions, timeouts, and cancellation cannot strand the CLI.
6. On completion or Ctrl+C, finalize state, flush storage, close browser/HTTP/database
   resources, and release the lock. Do not claim an in-flight remote action was
   undone merely because local execution was cancelled.

The current queue is a single-consumer queue, not a broadcast event bus. The CLI
owns one consumer; persistence happens at a shared publication boundary rather
than through a second consumer competing for the same queue. A safe event envelope
includes schema version, run ID, sequence, type, and timestamp.

Active tasks remain process-bound. On the next startup, historical runs left active
after a crash become interrupted. History is not automatic workflow resumption.
Never replay a payment, message, or calendar write solely from an unfinished run.

## Local state and security

Use platform-appropriate per-user directories, independent of the working directory
and installed package location. On Linux the layout is:

- `$XDG_CONFIG_HOME/agentgate/config.toml`, default `~/.config/agentgate/config.toml`.
- `$XDG_DATA_HOME/agentgate/state.sqlite3`, default `~/.local/share/agentgate/state.sqlite3`.
- Private artifacts beneath the data directory. Automated retention is deferred.

SQLite stores non-secret run summaries, sanitized event/audit records, connector
metadata, remote IDs, payment observations with checked-at times, and execution
intents. Preserve audit's final write-once-per-action contract; use a separate intent
journal to record an approved operation before its external effect and its outcome
afterward. If durable intent recording fails, do not perform a write.

Persist stable operation identity and idempotency data before financial requests.
The intent journal must not contain customer email, passwords, tokens, or raw browser
input. An ambiguous remote outcome stays unknown until reconciled. Local storage
is not tamper-proof against the machine owner and does not provide distributed
exactly-once execution.

Store credentials in an available secure OS keychain. SQLite contains references
and token scope/expiry metadata, not credential values. If no secure backend exists,
allow an explicitly chosen environment or session-only provider; never silently
fall back to plaintext. Do not accept secret values as command-line arguments.

Apply sanitization consistently before events, traces, audit, prompts-to-history,
and logs are persisted or rendered. Existing masking is a starting point, not a
guarantee that every arbitrary prompt, connector error, or artifact is safe.

Configuration precedence: command options, supported environment overrides,
an explicitly selected config file (or the user config), defaults. Project configuration is non-secret
and cannot lower trusted guardrail rules or silently expand filesystem access.
Require an explicit allowed workspace root and validate resolved paths, including
symlinks. Browser cookies are ephemeral in v1; persistent profiles are deferred.

## CLI surface

Initial commands:

```bash
agentgate init
agentgate doctor
agentgate connect github
agentgate connect gmail
agentgate connect calendar
agentgate connect stripe
agentgate disconnect calendar
agentgate setup browser
agentgate run "Read today's calendar and summarize it"
agentgate history
agentgate show <run-id>
agentgate payments sync
```

`init` writes non-secret configuration and initializes local storage. `doctor`
performs safe diagnostics without displaying credentials. `connect stripe` collects
a user-supplied test-mode key through hidden input; it is not Stripe Connect OAuth.

Human output is the default. `run --json --non-interactive` emits versioned,
sanitized NDJSON on stdout and diagnostics on stderr. Required approval or input
fails closed with an interaction-required result; there is no global `--yes`
guardrail bypass. Exit codes: 0 success, 1 failure/blocked/declined,
2 invalid usage/configuration, 3 interaction required, 130 user interruption.

No `serve`, background-run, cross-process `respond`, or automatic `resume` commands
in v1. After the command exits, `show` reads history, not a live engine.

## Integrations without an application server

### OAuth

Use the system browser and a temporary listener bound only to a loopback IP for
the authorization callback. Verify state, expire pending requests, close the
listener after completion, and use PKCE for supported native flows. This short-lived
callback receiver is not a persistent AgentGate API server. Google supports this
flow for Desktop app registrations. See [Google's installed-app guide](https://developers.google.com/identity/protocols/oauth2/native-app)
and [native OAuth security guidance](https://www.rfc-editor.org/rfc/rfc8252.html).

Do not ship confidential web-app secrets as if a distributed CLI could protect
them. Handle registration and supported authorization flow per provider. Start
GitHub with user-supplied tokens if an appropriate native authorization flow is
not configured. Request only required scopes and expose disconnect/revocation.

### Stripe

The local release supports guarded test-mode checkout/refund requests and explicit
status reconciliation, not an unattended payment fulfillment service. Keep the
catalog allowlist, trusted financial risk classification, and approval requirements.
Open hosted Checkout in the user's browser so card details stay outside AgentGate.

Use configured valid success/cancel URLs; require configuration instead of opening
the current nonexistent localhost landing pages. A redirect is never proof of payment.
`payments sync` retrieves tracked sessions/refunds and saves the observed provider
status and timestamp. Bounded opt-in polling may help an active user, but nothing
updates while the CLI is closed. Session completion and paid status are distinct.
See [Checkout Session retrieval](https://docs.stripe.com/api/checkout/sessions/retrieve).

Keep retries tied to the persisted logical operation and unchanged request inputs,
using [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests). Do not
assume an old key protects a retry indefinitely; reconcile unknown outcomes before
allowing another financial attempt.

Polling is a limited local status tool, not a replacement for production webhooks.
Reliable automated fulfillment and asynchronous payment handling belong to the
future server's signed webhook/reconciliation path, as explained in
[Stripe's fulfillment guide](https://docs.stripe.com/checkout/fulfillment).

### Other channels

GitHub, Gmail, Calendar, local file access, and browser automation can run through
outbound calls or local execution. Telegram outbound actions may use the same
connector, but its inbound webhook channel is outside the first local release.

## Packaging and delivery

Keep Python 3.11+ and the existing `app` import package initially. Use standard-library
argument parsing for the first CLI. Entry point:

```toml
[project.scripts]
agentgate = "app.cli.main:main"
```

Build a wheel installable from source or a release artifact with pipx. Publication
to a package index and a standalone executable are separate future decisions.
See the [Python CLI packaging guide](https://packaging.python.org/en/latest/guides/creating-command-line-tools/).

Keep core, SQLite, configuration, HTTP client, and secure credential support in
the base installation. Move Playwright to a `browser` extra, Stripe to a `stripe`
extra, and FastAPI/Uvicorn/PostgreSQL drivers/Redis to a `server` extra. Extras only
work after eager imports are removed. Browser installation is an explicit setup
step, never a surprise download during `--help` or the first run.

Implement in vertical slices:

1. Extract runtime dependencies and event/interaction seams while keeping HTTP
   behavior working. Add no-server import smoke tests.
2. Add SQLite migrations/repositories, private data paths, credentials, and intent
   persistence. Prove a local file run requires no PostgreSQL or Redis.
3. Add the entry point, foreground event rendering, approvals, clarification,
   cancellation, history, and non-interactive behavior.
4. Add desktop OAuth, browser bootstrap, and test-mode Stripe reconciliation.
5. Build and install a wheel in a clean environment outside the repository;
   document setup, supported capabilities, and deferred server features.

Acceptance: fresh-install `--help` works with no provider configuration; a file run
works without external infrastructure; approvals remain action-specific; secrets
never enter history; cancellation cleans up; crash recovery never auto-replays
writes; historical records survive exit; optional integrations fail with actionable
setup messages; and the local schema upgrades without running PostgreSQL SQL.

## Deferred server development

Add an HTTP adapter and ServerRuntime when remote/multi-user execution is needed.
Reuse the application service, domain policies, connector contracts, and safe event
schema. Add server authentication and ownership, durable workflow/approval state,
workers, broadcast/replayable events, managed secrets, and inbound webhooks then.

Do not build those systems into the local MVP, and do not promise local-to-server
database synchronization or seamless workflow migration in this architecture.
