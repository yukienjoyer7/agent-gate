"""Run-scoped Playwright browser sessions.

The reactive agent may execute several browser batches for one run.  Keeping
the Playwright context alive for that run preserves cookies, the current page,
and form state between those batches.  One-off browser API calls do not use
this manager and retain their existing ephemeral lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import async_playwright

from app.config.settings import get_settings
from app.domains.browser.browser_profile import DEFAULT_EXTRA_HEADERS, user_agent

logger = logging.getLogger(__name__)


@dataclass
class BrowserSession:
    """One browser context/page owned by a single Agent-Gate run."""

    run_id: str
    playwright: Any
    browser: Any
    context: Any
    page: Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    initialized: bool = False
    closed: bool = False

    async def ensure_page(
        self,
        *,
        url: str,
        wait_until: str,
        timeout_ms: int,
        navigate: bool,
    ) -> None:
        """Open a new URL, but preserve an already-open page on replan."""
        if self.closed:
            raise RuntimeError(f"browser session is already closed: {self.run_id}")
        if not self.initialized or (navigate and self.page.url != url):
            await self.page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            self.initialized = True

    async def close(self) -> None:
        """Close all Playwright resources, tolerating partial startup/cleanup."""
        if self.closed:
            return
        self.closed = True
        for resource_name, resource in (
            ("context", self.context),
            ("browser", self.browser),
        ):
            try:
                if resource is not None:
                    await resource.close()
            except Exception:
                logger.debug(
                    "could not close browser %s for run %s",
                    resource_name,
                    self.run_id,
                    exc_info=True,
                )
        try:
            if self.playwright is not None:
                await self.playwright.stop()
        except Exception:
            logger.debug("could not stop Playwright for run %s", self.run_id, exc_info=True)


class BrowserSessionManager:
    """Bounded in-memory registry of active run-scoped browser sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, run_id: str) -> BrowserSession:
        async with self._lock:
            existing = self._sessions.get(run_id)
            if existing is not None and not existing.closed:
                return existing

            settings = get_settings()
            max_sessions = getattr(settings, "BROWSER_MAX_CONCURRENT_SESSIONS", 10)
            if len(self._sessions) >= max_sessions:
                raise RuntimeError("maximum concurrent browser sessions reached")

            playwright = None
            browser = None
            context = None
            try:
                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(
                    headless=settings.PLAYWRIGHT_HEADLESS,
                    args=[
                        "--disable-http2",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                context = await browser.new_context(
                    user_agent=user_agent(),
                    extra_http_headers=DEFAULT_EXTRA_HEADERS,
                )
                page = await context.new_page()
            except Exception:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        logger.debug("could not close failed browser context", exc_info=True)
                elif browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        logger.debug("could not close failed browser", exc_info=True)
                if playwright is not None:
                    try:
                        await playwright.stop()
                    except Exception:
                        logger.debug("could not stop failed Playwright", exc_info=True)
                raise

            session = BrowserSession(
                run_id=run_id,
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
            )
            self._sessions[run_id] = session
            return session

    async def close(self, run_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(run_id, None)
        if session is None:
            return
        async with session.lock:
            await session.close()

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            async with session.lock:
                await session.close()

    def has(self, run_id: str) -> bool:
        session = self._sessions.get(run_id)
        return session is not None and not session.closed


browser_session_manager = BrowserSessionManager()


async def close_browser_session(run_id: str) -> None:
    """Close the session associated with a completed/cancelled run."""
    await browser_session_manager.close(run_id)
