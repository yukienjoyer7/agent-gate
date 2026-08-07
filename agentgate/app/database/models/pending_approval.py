"""
ORM model for the `pending_approvals` table.

Source of truth: app/database/migrations/versions/0002_create_pending_approvals.py

Design is the working queue described in pending-approval-design.md:
  - Deliberately mutable (no immutability trigger, unlike audit_logs) --
    rows are deleted once an action resolves (approved/rejected/expired).
  - A given action_id lives in at most one of {pending_approvals,
    audit_logs} at any time -- see the design doc's invariant in section 4.
  - `expires_at` is computed at insert time from
    settings.APPROVAL_TTL_MINUTES, not hardcoded, per design doc open
    question 2.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class PendingApproval(Base):
    __tablename__ = "pending_approvals"

    action_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    request_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    decision_json: Mapped[dict] = mapped_column(JSONB, nullable=False)

    pending_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
