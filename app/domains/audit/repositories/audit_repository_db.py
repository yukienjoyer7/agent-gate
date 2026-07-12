"""
Postgres-backed AuditRepository. ACTION-SOURCED.

Sourcing / design notes:
  - This is NOT a new schema. It writes to the `audit_logs` table already
    defined in app/database/migrations/versions/0001_create_audit_logs.py,
    which was previously created but never wired to a runtime repository
    (Sprint 1/2 shipped with the JSONL AuditRepository as the running
    store; see sprint.md DE-S1-02).
  - One row per action_id (action_id is UNIQUE at the DB level -- see
    0001). write() is called exactly once, after request + decision +
    execution are all known -- same call shape as the existing JSONL
    AuditRepository.write(), so callers (guarded_execution.py, api/v1/*)
    do not need to change how they call it, only which repository they
    import.
  - Immutability is enforced by the DB trigger from 0001
    (trg_audit_logs_immutable) -- this repository intentionally exposes
    no update/delete methods at all, on top of that DB-level guarantee.
  - This class intentionally mirrors the public method names of
    app/domains/audit/repositories/audit_repository.py (write, latest,
    list, by_run, by_action) so it's a drop-in replacement behind the
    same import site (app/domains/audit/repositories/__init__.py) --
    swapping stores should not require touching call sites.
  - Async throughout, using the existing AsyncSession from
    app/database/session.py (already configured for postgresql+psycopg).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.schemas import ActionRequest, AuditEvent, DecisionResponse, ExecutionResult
from app.database.models.audit_log import AuditLog
from app.database.session import SessionLocal


@asynccontextmanager
async def _reuse(session: AsyncSession):
    yield session


class AuditRepositoryDB:
    """
    session=None (default): opens/closes its own AsyncSession per call via
    SessionLocal, so this is constructible as AuditRepositoryDB() with no
    args -- same ergonomics as the JSONL AuditRepository(), no FastAPI
    Depends() wiring required at call sites.

    session=<AsyncSession>: pass an existing session (e.g. from
    app.api.dependencies.get_session) to participate in a caller-managed
    session/transaction, e.g. inside a FastAPI route or a test fixture.
    """

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    def _scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return _reuse(self._session) if self._session is not None else SessionLocal()

    async def write(
        self,
        request: ActionRequest,
        decision: DecisionResponse,
        execution: ExecutionResult,
        latency: dict[str, int] | None = None,
    ) -> AuditEvent:
        """
        Build one AuditEvent (pydantic) from the full action lifecycle and
        persist it as a single row. Mirrors AuditRepository.write() exactly,
        including the same field derivations (error_type from
        execution.error, default latency shape), so the two repositories
        produce identical AuditEvent payloads -- only the storage backend
        differs.
        """
        event = AuditEvent(
            run_id=request.run_id,
            action_id=request.action_id,
            request_json=request.model_dump(mode="json", exclude={"payload"}),
            decision_json=decision.model_dump(mode="json"),
            execution_json=execution.model_dump(mode="json"),
            execution_status=execution.status,
            error_type=(execution.error or {}).get("code"),
            latency=latency
            or {"guardrail_ms": decision.latency_ms, "executor_ms": execution.latency_ms},
        )

        row = AuditLog(
            audit_id=event.audit_id,
            run_id=event.run_id,
            action_id=event.action_id,
            request_json=event.request_json,
            decision_json=event.decision_json,
            execution_json=event.execution_json,
            execution_status=event.execution_status.value,
            error_type=event.error_type,
            policy_version=event.policy_version,
            detector_version=event.detector_version,
            latency=event.latency,
        )
        async with self._scope() as session:
            session.add(row)
            await session.commit()
        return event

    async def latest(self) -> AuditEvent | None:
        async with self._scope() as session:
            result = await session.execute(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(1)
            )
            row = result.scalar_one_or_none()
        return _to_audit_event(row) if row else None

    async def list(self) -> list[AuditEvent]:
        async with self._scope() as session:
            result = await session.execute(select(AuditLog).order_by(AuditLog.created_at.asc()))
            rows = result.scalars().all()
        return [_to_audit_event(row) for row in rows]

    async def by_run(self, run_id: str) -> list[AuditEvent]:
        async with self._scope() as session:
            result = await session.execute(
                select(AuditLog)
                .where(AuditLog.run_id == run_id)
                .order_by(AuditLog.created_at.asc())
            )
            rows = result.scalars().all()
        return [_to_audit_event(row) for row in rows]

    async def by_action(self, action_id: str) -> AuditEvent | None:
        """
        action_id is UNIQUE in audit_logs (see migration 0001), so this is
        a direct point lookup -- not a "first matching event" scan like the
        JSONL version's by_action(), which only happens to work because
        JSONL is also currently action-sourced (one line per action).
        """
        async with self._scope() as session:
            result = await session.execute(select(AuditLog).where(AuditLog.action_id == action_id))
            row = result.scalar_one_or_none()
        return _to_audit_event(row) if row else None


def _to_audit_event(row: AuditLog) -> AuditEvent:
    return AuditEvent(
        audit_id=row.audit_id,
        run_id=row.run_id,
        action_id=row.action_id,
        request_json=row.request_json,
        decision_json=row.decision_json,
        execution_json=row.execution_json,
        execution_status=row.execution_status,
        error_type=row.error_type,
        policy_version=row.policy_version,
        detector_version=row.detector_version,
        latency=row.latency,
        created_at=row.created_at,
    )
