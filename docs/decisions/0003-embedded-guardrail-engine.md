# ADR 0003: Embed the upstream guardrail core

- Status: Accepted
- Date: 2026-09-17

## Context

The application has the local CLI, API, connectors, interaction flow, and durable
history. NafisNaufal/agentgate has six Ollama detectors, validated declarative
policies, risk scoring, and sanitization. Their schemas and CLI/package names
collide, and upstream's PostgreSQL audit default conflicts with ADR 0002.

## Decision

Import the core at revision `9b919c604d2a0955fff26b4f05045281514c0220` into a private
vendored namespace. Adapt the application action contract and trusted connector
metadata to the engine, then map its result back with existing policy floors.
Select it by default through the existing sync/async decision entry points.
Retain `GUARDRAIL_BACKEND=legacy` as an explicit compatibility option.

Inject a durable evaluation journal into the engine. Every decision is recorded
before it reaches execution, separately from the existing final action audit.
Reuse the current planner, approval loop, and executors. Re-evaluate structured
redactions and clarified actions before execution.

## Consequences

Guarded runs now require Ollama; detector failure holds actions for approval.
No new Python dependencies, console command, PostgreSQL requirement for the CLI,
or database migrations are introduced. Server deployments must persist the
evaluation journal. The embedded source and host adapter can be reviewed and
updated independently. See the [integration guide](../integrations/guardrail.md)
for scope, configuration, and verification.
