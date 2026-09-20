# Contributing

## Setup

```bash
git clone <repo> && cd agent-gate
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,server,browser,stripe]"
cp .env.example .env
pytest
```

## Workflow

- Branch off `main`; never commit to `main`. Name branches `<type>/<short-description>`
  (`feat/approval-queue`, `fix/audit-timestamp`).
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `refactor:`,
  `test:`, `chore:`. Keep commits small and focused.
- One concern per pull request. Describe the change, how you tested it, and any migration or config impact.
- Record significant architectural decisions as an ADR in `docs/decisions/`.
- At least one review approval; squash-merge into `main`.

## Before you push

```bash
ruff check .
black --check .          # `black .` to fix
mypy app
pytest
```

Line length is 100 (ruff and black). The vendored guardrail (`app/domains/guardrail/_vendor`) is excluded from
lint and formatting; do not reformat it. It is a pinned copy of an upstream project; see its `NOTICE.md`.

## Where code goes

| Change | Location |
|--------|----------|
| New HTTP endpoint | `app/api/v1/`, with logic in a domain service (never in the router) |
| Business logic | `app/domains/<domain>/` |
| Shared contracts | `app/core/` (bump `schema_version` for breaking changes) |
| New connector | `app/domains/connector/<name>/`; follow [connectors.md](connectors.md#adding-a-connector) |
| Guardrail rules | Policy pack JSON, or `decision/` for host rules |
| Config | `app/config/settings.py` plus `.env.example` and [configuration.md](configuration.md) |
| Migrations | `alembic revision --autogenerate -m "..."`; import new models in `app/database/models/__init__.py` |

## Rules of the road

- Every proposed action becomes an `ActionRequest` before execution, and every executed or skipped action is audited.
- Never log secrets, tokens, or private payloads. `ActionRequest.payload` must stay out of serialized records.
- Keep domains decoupled; cross-domain calls go through services or the shared contracts.
- Prefer failing closed: unknown operations are blocked, detector errors require approval.
- Never commit secrets. Only `.env.example` is meant to be tracked (`.env.development` and `.env.staging` also
  are, so keep them free of real values).

## Documentation

Update the relevant page in `docs/` in the same pull request: API changes in `api-reference.md`, settings in
`configuration.md`, schema changes in `contracts.md` and `data-model.md`, connector changes in `connectors.md`.
