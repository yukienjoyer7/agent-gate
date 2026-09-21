"""add Telegram account ownership and one-time linking tokens

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op  # type: ignore[import-untyped]

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("telegram_contacts", sa.Column("owner_id", sa.String(length=255), nullable=True))
    op.add_column(
        "telegram_contacts", sa.Column("telegram_user_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "telegram_contacts",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="observed"),
    )
    op.add_column(
        "telegram_contacts", sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "telegram_contacts",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_telegram_contacts_owner_id", "telegram_contacts", ["owner_id"])
    op.create_index(
        "ix_telegram_contacts_telegram_user_id", "telegram_contacts", ["telegram_user_id"]
    )
    op.create_index(
        "uq_telegram_contacts_connected_owner",
        "telegram_contacts",
        ["owner_id"],
        unique=True,
        postgresql_where=sa.text("status = 'connected'"),
    )
    op.create_index(
        "uq_telegram_contacts_connected_user",
        "telegram_contacts",
        ["telegram_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'connected'"),
    )

    op.create_table(
        "telegram_link_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_telegram_link_tokens_token_hash", "telegram_link_tokens", ["token_hash"])
    op.create_index("ix_telegram_link_tokens_owner_id", "telegram_link_tokens", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_telegram_link_tokens_owner_id", table_name="telegram_link_tokens")
    op.drop_index("ix_telegram_link_tokens_token_hash", table_name="telegram_link_tokens")
    op.drop_table("telegram_link_tokens")
    op.drop_index("uq_telegram_contacts_connected_user", table_name="telegram_contacts")
    op.drop_index("uq_telegram_contacts_connected_owner", table_name="telegram_contacts")
    op.drop_index("ix_telegram_contacts_telegram_user_id", table_name="telegram_contacts")
    op.drop_index("ix_telegram_contacts_owner_id", table_name="telegram_contacts")
    op.drop_column("telegram_contacts", "updated_at")
    op.drop_column("telegram_contacts", "connected_at")
    op.drop_column("telegram_contacts", "status")
    op.drop_column("telegram_contacts", "telegram_user_id")
    op.drop_column("telegram_contacts", "owner_id")
