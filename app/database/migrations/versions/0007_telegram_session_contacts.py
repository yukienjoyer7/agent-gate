"""add session-scoped Telegram contacts and invitations

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op  # type: ignore[import-untyped]

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_session_contacts",
        sa.Column("contact_id", sa.String(length=32), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("browser_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=512), nullable=True),
        sa.Column("alias", sa.String(length=255), nullable=False),
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
        sa.UniqueConstraint("session_id", "chat_id", name="uq_telegram_session_contacts_chat"),
    )
    op.create_index(
        "ix_telegram_session_contacts_session_id", "telegram_session_contacts", ["session_id"]
    )
    op.create_table(
        "telegram_contact_invites",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("browser_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_telegram_contact_invites_token_hash",
        "telegram_contact_invites",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_telegram_contact_invites_session_id",
        "telegram_contact_invites",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_telegram_contact_invites_session_id", table_name="telegram_contact_invites")
    op.drop_index("ix_telegram_contact_invites_token_hash", table_name="telegram_contact_invites")
    op.drop_table("telegram_contact_invites")
    op.drop_index("ix_telegram_session_contacts_session_id", table_name="telegram_session_contacts")
    op.drop_table("telegram_session_contacts")
