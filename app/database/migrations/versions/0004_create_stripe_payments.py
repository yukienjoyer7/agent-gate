"""create Stripe payment and webhook event tables

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op  # type: ignore[import-untyped]

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stripe_payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("stripe_session_id", sa.String(length=255), nullable=True, unique=True),
        sa.Column("payment_intent_id", sa.String(length=255), nullable=True, unique=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("action_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=12), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment="Safe Stripe reconciliation fields; no card data or API credentials",
    )
    op.create_index(
        "ix_stripe_payments_stripe_session_id", "stripe_payments", ["stripe_session_id"]
    )
    op.create_index(
        "ix_stripe_payments_payment_intent_id", "stripe_payments", ["payment_intent_id"]
    )
    op.create_index("ix_stripe_payments_run_id", "stripe_payments", ["run_id"])
    op.create_index("ix_stripe_payments_action_id", "stripe_payments", ["action_id"])

    op.create_table(
        "stripe_webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("stripe_event_id", sa.String(length=255), nullable=False, unique=True),
        sa.Column("event_type", sa.String(length=255), nullable=False),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment="Deduplication ledger for signed Stripe webhook events",
    )
    op.create_index("ix_stripe_webhook_events_event_type", "stripe_webhook_events", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_stripe_webhook_events_event_type", table_name="stripe_webhook_events")
    op.drop_table("stripe_webhook_events")
    op.drop_index("ix_stripe_payments_action_id", table_name="stripe_payments")
    op.drop_index("ix_stripe_payments_run_id", table_name="stripe_payments")
    op.drop_index("ix_stripe_payments_payment_intent_id", table_name="stripe_payments")
    op.drop_index("ix_stripe_payments_stripe_session_id", table_name="stripe_payments")
    op.drop_table("stripe_payments")
