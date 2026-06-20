# ADR 0001 — Architecture Foundation

- **Status:** Accepted
- **Date:** 2026-06
- **Context:** Sprint 0 — Data Engineering Foundation

## Decision

| Area | Decision |
|------|----------|
| Architecture Style | Modular Monolith |
| Backend Framework | FastAPI |
| Language | Python 3.11 |
| Database | PostgreSQL 17 (JSONB for semi-structured payloads) |
| ORM | SQLAlchemy 2.0 |
| Migration Tool | Alembic |
| Browser Automation | Playwright |
| Queue Layer | Redis (future-ready) |
| Containerization | Docker & Docker Compose |
| Secrets (MVP) | `.env` via pydantic-settings |
| Secrets (Prod) | AWS Secrets Manager / HashiCorp Vault |

## Rationale

A Modular Monolith keeps the MVP simple while preserving clean domain boundaries
(agent, guardrail, approval, audit, connector, browser, benchmark) so that
`audit`, `approval`, `connector`, and `benchmark` can later be extracted into
independent services. `agent`, `guardrail`, `browser`, and `llm` stay Python-coupled
to the AI tooling and Playwright ecosystem.

## Consequences

- Single deployable unit for MVP; horizontal scaling later via Redis queue + workers.
- All connectors implement a shared `BaseConnector.execute(action, payload)` contract.
- Logs are structured JSON for observability, auditability, and benchmark analysis.
