"""
ORM model for the `audit_logs` table.

Source of truth: app/database/migrations/versions/0001_create_audit_logs.py
This model is a 1:1 mapping of that migration -- every column name/type here
matches what 0001 already creates. No new schema is introduced.

Design is ACTION-SOURCED, not event-sourced:
  - action_id is unique -> exactly one row per action, written once its
    full lifecycle (request -> decision -> execution) is known.
  - This matches app/core/audit_schema.py:AuditEvent, which is the pydantic
    model the existing JSONL-based AuditRepository already writes.
  - Immutability (no UPDATE/DELETE) is enforced by the DB trigger created
    in migration 0001 (trg_audit_logs_immutable), not by this model.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    audit_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

    request_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    decision_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    execution_json: Mapped[dict] = mapped_column(JSONB, nullable=False)

    execution_status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    policy_version: Mapped[str] = mapped_column(String(32), server_default="policy-0.1")
    detector_version: Mapped[str] = mapped_column(String(32), server_default="detector-0.1")

    latency: Mapped[dict] = mapped_column(JSONB, server_default="{}")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
