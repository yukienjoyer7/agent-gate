"""
Repository for the `pending_user_questions` working queue.

Mirrors app/domains/approval/repositories/pending_approval_repository.py --
same mutable-queue / atomic-claim shape, applied to the ASK_USER decision
path instead of NEED_APPROVAL. See that module's docstring for the
reasoning behind claim() as the single atomic DELETE...RETURNING
"resolve" primitive; it applies unchanged here.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.core.schemas import ActionRequest, DecisionResponse
from app.database.models.pending_user_question import PendingUserQuestion
from app.database.session import SessionLocal


@asynccontextmanager
async def _reuse(session: AsyncSession):
    yield session


class PendingUserQuestionRepository:
    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    def _scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return _reuse(self._session) if self._session is not None else SessionLocal()

    async def create(
        self,
        request: ActionRequest,
        decision: DecisionResponse,
        ttl_minutes: int | None = None,
        clarification_round: int = 1,
    ) -> PendingUserQuestion:
        now = datetime.now(UTC)
        ttl = ttl_minutes if ttl_minutes is not None else get_settings().ASK_USER_TTL_MINUTES
        row = PendingUserQuestion(
            action_id=request.action_id,
            run_id=request.run_id,
            question=decision.clarifying_question or "This action needs more information before it can proceed. Can you clarify or complete the missing details?",
            # ActionRequest.payload is Field(exclude=True), so plain
            # model_dump() drops it -- added back explicitly here, same as
            # pending_approvals, since a proceed=True response needs the
            # original payload to merge corrections into and execute.
            request_json={**request.model_dump(mode="json"), "payload": request.payload},
            decision_json=decision.model_dump(mode="json"),
            clarification_round=clarification_round,
            pending_since=now,
            expires_at=now + timedelta(minutes=ttl),
        )
        async with self._scope() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def list(self) -> list[PendingUserQuestion]:
        async with self._scope() as session:
            result = await session.execute(
                select(PendingUserQuestion).order_by(PendingUserQuestion.pending_since.asc())
            )
            return list(result.scalars().all())

    async def get(self, action_id: str) -> PendingUserQuestion | None:
        async with self._scope() as session:
            result = await session.execute(
                select(PendingUserQuestion).where(PendingUserQuestion.action_id == action_id)
            )
            return result.scalar_one_or_none()

    async def claim(self, action_id: str) -> PendingUserQuestion | None:
        """
        Atomically remove and return the row for action_id, or None if it
        doesn't exist (already resolved by a concurrent call, expired and
        already swept, or never existed). Whether the claimed row was
        actually expired is for the caller to check against expires_at --
        this method just performs the delete-and-return.
        """
        async with self._scope() as session:
            result = await session.execute(
                delete(PendingUserQuestion)
                .where(PendingUserQuestion.action_id == action_id)
                .returning(PendingUserQuestion)
            )
            row = result.scalars().first()
            await session.commit()
        return row
