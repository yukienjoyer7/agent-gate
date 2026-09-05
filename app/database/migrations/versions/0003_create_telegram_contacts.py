"""create telegram contacts

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op  # type: ignore[import-untyped]  # Alembic ships without typing metadata.

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_contacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("chat_type", sa.String(length=32), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=512), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment="Telegram identities learned from inbound Bot API updates; no credentials",
    )
    op.create_index("ix_telegram_contacts_username", "telegram_contacts", ["username"])
    op.create_index("ix_telegram_contacts_display_name", "telegram_contacts", ["display_name"])


def downgrade() -> None:
    op.drop_index("ix_telegram_contacts_display_name", table_name="telegram_contacts")
    op.drop_index("ix_telegram_contacts_username", table_name="telegram_contacts")
    op.drop_table("telegram_contacts")
