# Documentation index

## Use it
| Document | Purpose |
|----------|---------|
| [getting-started.md](getting-started.md) | Prerequisites, install, configure, run, verify (CLI, server, Docker) |
| [cli.md](cli.md) | Local CLI guide: commands, approvals, exit codes, credential stores |
| [api-reference.md](api-reference.md) | Endpoints, statuses, SSE events, webhook auth |
| [configuration.md](configuration.md) | Every environment variable |
| [connectors.md](connectors.md) | What each connector can do and how to set it up |

## Understand it
| Document | Purpose |
|----------|---------|
| [architecture.md](architecture.md) | Components, data flow, agent loop, module map |
| [guardrail-policy.md](guardrail-policy.md) | How a verdict is produced, policy packs, sensitive input |
| [contracts.md](contracts.md) | Runtime schemas (v0.1) |
| [data-model.md](data-model.md) | Database tables and local SQLite/JSONL stores |
| [audit-design.md](audit-design.md) | Current audit model vs. the event-sourced target |
| [glossary.md](glossary.md) | Terms used across the docs |

## Run and secure it
| Document | Purpose |
|----------|---------|
| [deployment.md](deployment.md) | Deploy, migrate, monitor, back up, runbook |
| [security.md](security.md) | Threat model, trust boundaries, hardening checklist |
| [limitations.md](limitations.md) | Known gaps and inconsistencies |

## Build on it
| Document | Purpose |
|----------|---------|
| [contributing.md](contributing.md) | Workflow, where code goes, adding a connector |
| [testing.md](testing.md) | Test layout, fixtures, how to run and write tests |
| [decisions/](decisions/) | ADRs: 0001 foundation, [0002 local-first CLI](decisions/0002-local-first-cli.md), [0003 embedded guardrail engine](decisions/0003-embedded-guardrail-engine.md), [0004 audit granularity](decisions/0004-audit-granularity.md) (proposed; would amend 0002's write-once audit) |

## Integration guides (existing)
[integrations/guardrail.md](integrations/guardrail.md), [integrations/stripe.md](integrations/stripe.md),
[integrations/telegram.md](integrations/telegram.md), [CHANGELOG-atomic-browser-audit.md](CHANGELOG-atomic-browser-audit.md).
