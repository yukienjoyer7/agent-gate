from typing import Literal

from app.config.settings import Settings


class DevelopmentSettings(Settings):
    """Environment-specific overrides for local development."""

    DEBUG: bool = True
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "DEBUG"
    PLAYWRIGHT_HEADLESS: bool = False
    AUDIT_RETENTION_DAYS: int = 7
    TRACE_RETENTION_DAYS: int = 7
    BROWSER_MAX_CONCURRENT_SESSIONS: int = 10
