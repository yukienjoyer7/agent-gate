# Local CLI

AgentGate runs the planner, guardrail, approvals, and connectors in one foreground
process. No FastAPI server, Docker, PostgreSQL, or Redis is required. Local-first
is not offline: prompts and relevant observations go to the configured external
LLM, and provider integrations make outbound API calls.

## Install

From this checkout, using Python 3.11 or newer:

```bash
pipx install .
agentgate --help
agentgate init
agentgate doctor
```

An alternative is installing a built wheel with pipx. This package is not assumed
to be published on PyPI. `python -m app` also provides the installed CLI.
For server development and the full test dependencies:

```bash
uv sync --extra dev --extra server --extra browser --extra stripe
```

`init` selects the LLM dialect, endpoint/model, timezone, explicitly allowed
workspace, and credential store. Hidden API-key input goes to the OS keychain,
not TOML. Repeated init preserves existing config. Headless setup:

```bash
agentgate init --non-interactive --workspace /absolute/path/to/project --credential-store env
```

Supply `LLM_API_KEY` securely through your calling environment. There are no
secret-valued command-line flags and no implicit repository `.env` loading.

The default guardrail requires Ollama and a detector model:

```bash
ollama pull qwen2.5:7b
# In the shell running Ollama:
OLLAMA_NUM_PARALLEL=6 ollama serve
agentgate doctor
```

If Ollama already runs as a service, configure its parallelism in that service.
On PowerShell, use `$env:OLLAMA_NUM_PARALLEL = "6"` before `ollama serve`.
Supported shell overrides are `OLLAMA_HOST`, `AGENTGATE_LLM_DETECTOR_MODEL`,
`AGENTGATE_LLM_FALLBACK_MODEL`, `AGENTGATE_LLM_FALLBACK_ATTEMPTS`,
`AGENTGATE_LLM_DETECTOR_TIMEOUT`, and `AGENTGATE_DETECTOR_ARCHITECTURE` (`six` or
experimental `unified`). These are independent of the planner configuration.
`GUARDRAIL_BACKEND=legacy` explicitly restores the earlier guardrail.
See [guardrail integration](integrations/guardrail.md) for behavior and audit details.

## Commands and interaction

| Command | Purpose |
| --- | --- |
| `agentgate init` | Guided setup and local schema initialization |
| `agentgate doctor` | Credential-presence, workspace, storage, browser, and detector readiness checks |
| `agentgate connect llm` | Store an LLM key through hidden input |
| `agentgate connect github` | Store a user-supplied GitHub token |
| `agentgate connect gmail` | Google Desktop OAuth for Gmail read access |
| `agentgate connect calendar` | Google Desktop OAuth for Calendar event access |
| `agentgate connect stripe` | Store a supplied Stripe test-mode key |
| `agentgate disconnect <provider>` | Remove credentials; revoke Google OAuth first |
| `agentgate setup browser` | Explicitly download Chromium and enable browser execution |
| `agentgate run "<task>"` | Execute a foreground guarded run |
| `agentgate history --limit 20` | Read previous run summaries |
| `agentgate show <run-id>` | Read saved steps, events, and audit |
| `agentgate payments sync` | Retrieve tracked Stripe session/refund status |

Common options, before or after the command: `--config /path/config.toml`,
`--credential-store keyring|env|session`, and `--json`. `run` also accepts
`--workspace`; `run` and `init` accept `--non-interactive`.

```bash
agentgate connect calendar
agentgate run "Create a 30-minute calendar event tomorrow at 10am called Demo"
```

Missing information prompts for structured clarification before approval. Approval
shows the action, target, payload, risk, and policy reasons. Only `y`/`yes` approves;
other responses decline. Secret/sanitize fields use hidden input. Each replanned
write independently passes policy and approval. Clarification is not approval of
a Calendar or financial write. One active run is allowed per data profile.

Ctrl+C cancels and closes browser resources, but does not undo remote effects.
Abandoned runs are marked interrupted on the next runtime startup. History is not
resumption. No daemon, background execution, cross-process responses, `serve`,
or automatic `resume` is implemented.

```bash
agentgate run "Read README.md" --workspace /absolute/path/to/project --json --non-interactive
```

Run JSON output is sanitized NDJSON with schema version, run ID, sequence,
timestamp, type, and data. Diagnostics/logs use stderr. Required approval/input
fails closed with exit code 3; there is no `--yes` bypass. Rerunning creates a new run.

| Exit code | Meaning |
| --- | --- |
| 0 | Successful operation |
| 1 | Failed/blocked/declined run, failed diagnostics, unresolved reconciliation |
| 2 | Invalid usage/configuration or local storage setup |
| 3 | Terminal interaction required |
| 130 | User interruption |

## Local configuration and credentials

Linux defaults: `~/.config/agentgate/config.toml` and
`~/.local/share/agentgate/state.sqlite3`, honoring XDG variables. Other platforms
use standard per-user locations. Override `AGENTGATE_CONFIG_DIR` and
`AGENTGATE_DATA_DIR` to isolate a profile. Package/repository paths are not data paths.

SQLite stores sanitized history, safe audit/traces, OAuth metadata, payment status
with checked-at timestamps, and execution intents. Tokens/keys live outside SQLite.
The embedded engine additionally appends each evaluation to `guardrail.jsonl`
in the private data directory before returning a verdict. Final action audits
in SQLite reference the evaluation's `guardrail_audit_id`. Redacted content is
re-evaluated and shown for approval; it is not treated as missing secret input.
Audit is append-only through SQLite triggers, not tamper-proof against the owner.
Do not put credentials in prompts. Masking covers known credentials and recognized
secret fields/formats, not every arbitrary string that a user might consider secret.
Known credential values are also masked from outgoing LLM message context.

Credential modes:

- `keyring`: supported secure OS keychain backends only. Missing/locked keychains
  fail explicitly, with no silent plaintext fallback.
- `env`: explicitly read `LLM_API_KEY`, `GITHUB_TOKEN`, `GMAIL_ACCESS_TOKEN`,
  `GOOGLE_CALENDAR_ACCESS_TOKEN`, `STRIPE_SECRET_KEY`, and
  `GOOGLE_OAUTH_CLIENT_SECRET`. Connect checks presence; disconnect tells you to
  unset the variable. Environment credentials are never modified or written to files.
- `session`: run collects hidden credentials for that process only. Connect cannot
  persist session credentials.

Config fields: `llm_type`, `llm_url`, `llm_model`, `timezone`, `workspace`,
`credential_store`, `browser_enabled`, `google_client_id`, `stripe_success_url`,
`stripe_cancel_url`, and the `stripe_price_map` table. Unknown fields are rejected.
Project config is loaded only with explicit `--config`. It cannot lower mandatory
guardrail rules or add filesystem roots.

Non-secret environment overrides: `LLM_TYPE`, `LLM_URL`, `LLM_MODEL`,
`GOOGLE_OAUTH_CLIENT_ID`, `STRIPE_SUCCESS_URL`, `STRIPE_CANCEL_URL`, and
`STRIPE_PRICE_MAP` (JSON object). Timeout/step limits and optional guardrail-model
settings retain their existing environment names. Unrelated server variables are
ignored. The selected credential mode does not silently change because a variable exists.

## Optional integrations

### Browser

Install extras from the source checkout or an extra-enabled release wheel:

```bash
pipx install --force '.[browser]'
agentgate setup browser
agentgate doctor
```

Include all needed extras when reinstalling, for example `.[browser,stripe]`.
Setup explicitly downloads Chromium. OS browser libraries may also be needed;
AgentGate does not install system packages or request elevated access. Browser
state is ephemeral across runs. Screenshots go only under the profile artifact
directory, never to a model-selected path. Images can contain private page content;
inspect artifacts before sharing.

### Google and GitHub

Create a Google OAuth Desktop app registration and enable the Gmail/Calendar APIs.
Connect prompts for a Desktop client ID and, when required, its user-supplied secret.
The ID is non-secret config; the secret and OAuth tokens are stored in the OS
keychain. The system browser opens, a short-lived `127.0.0.1` listener receives
the state-checked PKCE callback, then closes. No persistent API server or public
callback is required. See [Google Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app).

Gmail requests read-only scope; Calendar requests event scope. OAuth refresh updates
the secret store. Environment mode uses supplied access tokens without persistent
OAuth refresh. Google revocation can also invalidate the other Google integration;
reconnect it if necessary. GitHub uses a supplied token for repository metadata.
No confidential web-app secret is bundled with the CLI.

### Stripe

```bash
pipx install --force '.[stripe]'
agentgate connect stripe
```

Configure valid landing URLs and an allowlisted test catalog in TOML. No nonexistent
localhost landing page is assumed. Place scalar fields before TOML table headers:

```toml
stripe_success_url = "https://your-merchant.example/success"
stripe_cancel_url = "https://your-merchant.example/cancel"

[stripe_price_map]
demo = "price_YOUR_TEST_PRICE_ID"
```

Plans choose catalog keys/quantities, not arbitrary prices or URLs. Financial writes
require test-mode keys and approval. Checkout URLs appear in terminal results;
open one in the system browser and enter card details on Stripe's hosted page,
not in AgentGate. Completion/redirects are not proof of payment.

Sync explicitly refreshes known sessions/refunds. Nothing updates while the CLI
is closed. The journal commits identity before effects. Equivalent completed writes
in the same run reuse results; unknown writes are blocked rather than replayed.
Known remote IDs remain recoverable even after payment-row persistence failure.
Unknown operations without IDs require manual provider reconciliation and remain
blocked; no unsafe clear-and-retry command exists. Reliable unattended fulfillment,
async payment notifications, and signed inbound webhooks belong to the server phase.
See [Stripe fulfillment requirements](https://docs.stripe.com/checkout/fulfillment).

## Development boundary

`AgentService` uses task-scoped `LocalRuntime` dependencies. Existing HTTP routes
retain their defaults; install the `server` extra to develop them. PostgreSQL
migration history is unchanged; SQLite uses its own versioned schema. See
[ADR 0002](./decisions/0002-local-first-cli.md).
