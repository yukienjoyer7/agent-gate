from typing import Literal

from app.config.settings import Settings


class StagingSettings(Settings):
    """Environment-specific overrides for staging / pre-production."""

    DEBUG: bool = False
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    PLAYWRIGHT_HEADLESS: bool = True
    AUDIT_RETENTION_DAYS: int = 30
    TRACE_RETENTION_DAYS: int = 14
    BROWSER_MAX_CONCURRENT_SESSIONS: int = 25
