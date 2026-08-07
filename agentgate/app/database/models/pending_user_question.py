"""
ORM model for the `pending_user_questions` table.

Source of truth: app/database/migrations/versions/0003_create_pending_user_questions.py

Mirrors PendingApproval (app/database/models/pending_approval.py): a
mutable working queue, not an audit record. Same invariant applies -- a
given action_id lives in at most one of {pending_user_questions,
pending_approvals, audit_logs} at any time. `question` is duplicated out
of decision_json's clarifying_question so callers (e.g. a UI listing
pending questions) don't need to parse the JSONB blob just to render the
prompt.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class PendingUserQuestion(Base):
    __tablename__ = "pending_user_questions"

    action_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    question: Mapped[str] = mapped_column(Text, nullable=False)
    request_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    decision_json: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Which round of clarification this row represents (1 = the original
    # ask). Incremented each time a user's response still doesn't clear the
    # confidence threshold and the action goes back into the queue rather
    # than failing closed. Bounded by MAX_CLARIFICATION_ROUNDS in
    # clarification_service.py.
    clarification_round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    pending_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
