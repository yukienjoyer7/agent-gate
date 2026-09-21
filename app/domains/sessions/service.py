"""Issue and expire opaque browser-scoped demo sessions."""

from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.config.settings import get_settings
from app.database.models.browser_session import BrowserSession

logger = logging.getLogger(__name__)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TOUCH_INTERVAL = timedelta(seconds=60)


def valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.fullmatch(session_id))


async def create_browser_session() -> str:
    """Create a high-entropy ID and persist its lifecycle record."""
    from app.database.session import SessionLocal

    session_id = secrets.token_urlsafe(32)
    async with SessionLocal() as db:
        db.add(BrowserSession(session_id=session_id))
        await db.commit()
    return session_id


async def touch_browser_session(session_id: str) -> bool:
    """Validate an active session and update its activity timestamp sparingly."""
    if not valid_session_id(session_id):
        return False

    from app.database.session import SessionLocal

    now = datetime.now(UTC)
    expired = False
    async with SessionLocal() as db:
        row = await db.scalar(
            select(BrowserSession)
            .where(BrowserSession.session_id == session_id)
            .with_for_update()
        )
        if row is None or row.ended_at is not None:
            return False

        last_seen_at = row.last_seen_at
        if last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=UTC)
        ttl = timedelta(seconds=get_settings().BROWSER_SESSION_IDLE_TTL_SEC)
        if last_seen_at < now - ttl:
            row.ended_at = now
            row.end_reason = "idle_timeout"
            expired = True
        elif last_seen_at < now - _TOUCH_INTERVAL:
            row.last_seen_at = now
        await db.commit()

    if expired:
        await cleanup_browser_session(session_id)
        return False
    return True


async def end_browser_session(session_id: str, reason: str) -> bool:
    """Mark a session ended once, retaining its database lifecycle record."""
    if not valid_session_id(session_id):
        return False

    from app.database.session import SessionLocal

    async with SessionLocal() as db:
        result = await db.execute(
            update(BrowserSession)
            .where(
                BrowserSession.session_id == session_id,
                BrowserSession.ended_at.is_(None),
            )
            .values(ended_at=datetime.now(UTC), end_reason=reason)
            .returning(BrowserSession.session_id)
        )
        ended = result.scalar_one_or_none() is not None
        await db.commit()

    if ended:
        await cleanup_browser_session(session_id)
    return ended


async def expire_idle_browser_sessions() -> int:
    """End inactive sessions; the caller may run this from a periodic task."""
    from app.database.session import SessionLocal

    cutoff = datetime.now(UTC) - timedelta(
        seconds=get_settings().BROWSER_SESSION_IDLE_TTL_SEC
    )
    async with SessionLocal() as db:
        result = await db.execute(
            update(BrowserSession)
            .where(
                BrowserSession.ended_at.is_(None),
                BrowserSession.last_seen_at < cutoff,
            )
            .values(ended_at=datetime.now(UTC), end_reason="idle_timeout")
            .returning(BrowserSession.session_id)
        )
        expired_ids = list(result.scalars().all())
        await db.commit()

    for session_id in expired_ids:
        await cleanup_browser_session(session_id)
    return len(expired_ids)


async def cleanup_browser_session(session_id: str) -> None:
    """Drop ephemeral run/chat state and the session's Telegram link."""
    from app.domains.agent.services.run_registry import run_registry

    run_registry.remove_by_owner_id(session_id)
    try:
        # Imported lazily to keep the session domain independent of API setup.
        from app.domains.connector.telegram.service import TelegramService

        await TelegramService().end_browser_owner_session(session_id)
    except Exception:  # noqa: BLE001 - expiry must still end the session
        logger.exception("could not disconnect Telegram for ended browser session")
