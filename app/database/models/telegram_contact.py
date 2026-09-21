"""Persistent Telegram identities learned from inbound Bot API updates.

This table deliberately stores no bot credential.  A contact is only an
identity the bot has already observed, which is the limit of what the Telegram
Bot API can safely address without a separate MTProto client.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class TelegramContact(Base):
    __tablename__ = "telegram_contacts"

    __table_args__ = (
        Index(
            "uq_telegram_contacts_connected_owner",
            "owner_id",
            unique=True,
            postgresql_where=text("status = 'connected'"),
        ),
        Index(
            "uq_telegram_contacts_connected_user",
            "telegram_user_id",
            unique=True,
            postgresql_where=text("status = 'connected'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    chat_type: Mapped[str] = mapped_column(String(32), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    # ``owner_id`` is the application identity abstraction used by the current
    # unauthenticated demo. Production deployments should populate it from
    # their authenticated principal, not from a user-supplied chat ID.
    owner_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="observed", default="observed"
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
