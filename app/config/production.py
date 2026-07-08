from typing import Literal

from app.config.settings import Settings


class ProductionSettings(Settings):
    """Environment-specific overrides for production."""

    DEBUG: bool = False
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    PLAYWRIGHT_HEADLESS: bool = True
    AUDIT_RETENTION_DAYS: int = 365
    TRACE_RETENTION_DAYS: int = 90
    SCREENSHOT_RETENTION_DAYS: int = 30
    BROWSER_MAX_CONCURRENT_SESSIONS: int = 50
