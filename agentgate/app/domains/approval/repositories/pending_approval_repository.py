"""
Repository for the `pending_approvals` working queue.

See pending-approval-design.md sections 3-5 for the full design. Key
points reflected here:

  - This table is deliberately mutable -- rows are deleted once an action
    resolves (approved/rejected/expired). It is a queue, not a compliance
    record; audit_logs is the compliance record, written once at
    resolution time by app/domains/approval/services.

  - claim() is the "resolve" primitive: a single `DELETE ... RETURNING`
    that atomically removes and returns the row. If two decide calls race
    for the same action_id, only one DELETE actually matches a row -- the
    other gets None back -- so there is no separate lock/transaction
    needed to answer design doc open question 4. Expiry is decided by the
    caller by comparing the returned row's expires_at to now(), not by
    claim() itself, so the same primitive backs both the "reviewer
    decides in time" (3a) and "reviewer decides too late" (3b) paths in
    the design doc, as well as lazy expiry from list_active() (step 2).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.core.schemas import ActionRequest, DecisionResponse
from app.database.models.pending_approval import PendingApproval
from app.database.session import SessionLocal


@asynccontextmanager
async def _reuse(session: AsyncSession):
    yield session


class PendingApprovalRepository:
    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    def _scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return _reuse(self._session) if self._session is not None else SessionLocal()

    async def create(
        self,
        request: ActionRequest,
        decision: DecisionResponse,
        ttl_minutes: int | None = None,
    ) -> PendingApproval:
        now = datetime.now(UTC)
        ttl = ttl_minutes if ttl_minutes is not None else get_settings().APPROVAL_TTL_MINUTES
        row = PendingApproval(
            action_id=request.action_id,
            run_id=request.run_id,
            # ActionRequest.payload is declared with Field(exclude=True),
            # so plain model_dump() drops it regardless (that's deliberate
            # for audit_logs' request_json, to keep raw payloads out of
            # the permanent log). Unlike audit_logs, this snapshot MUST
            # keep the payload -- it's what ExecutionRouter.execute()
            # needs to actually run the action for real if a reviewer
            # approves it later -- so it's added back in explicitly here.
            request_json={**request.model_dump(mode="json"), "payload": request.payload},
            decision_json=decision.model_dump(mode="json"),
            pending_since=now,
            expires_at=now + timedelta(minutes=ttl),
        )
        async with self._scope() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def list(self) -> list[PendingApproval]:
        async with self._scope() as session:
            result = await session.execute(
                select(PendingApproval).order_by(PendingApproval.pending_since.asc())
            )
            return list(result.scalars().all())

    async def get(self, action_id: str) -> PendingApproval | None:
        async with self._scope() as session:
            result = await session.execute(
                select(PendingApproval).where(PendingApproval.action_id == action_id)
            )
            return result.scalar_one_or_none()

    async def claim(self, action_id: str) -> PendingApproval | None:
        """
        Atomically remove and return the row for action_id, or None if it
        doesn't exist (already resolved by a concurrent call, expired and
        already swept, or never existed). Whether the claimed row was
        actually expired is for the caller to check against expires_at --
        this method just performs the delete-and-return.
        """
        async with self._scope() as session:
            result = await session.execute(
                delete(PendingApproval)
                .where(PendingApproval.action_id == action_id)
                .returning(PendingApproval)
            )
            row = result.scalars().first()
            await session.commit()
        return row
