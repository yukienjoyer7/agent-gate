from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment / .env files."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # Environment Identity
    APP_NAME: str = "AgentGate"
    APP_ENV: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Database
    DATABASE_URL: str = "postgresql+psycopg://agentgate:agentgate@localhost:5432/agentgate"
    DATABASE_POOL_SIZE: int = Field(default=10, ge=1, le=100)
    DATABASE_MAX_OVERFLOW: int = Field(default=20, ge=0)

    # Connector Credentials
    GITHUB_TOKEN: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    XENDIT_API_KEY: str = ""
    GMAIL_CREDENTIALS_PATH: str = "credentials/gmail_token.json"

    # Filesystem
    LOCAL_FILE_ROOT: str = "demo_data"
    ALLOWED_FILESYSTEM_PATHS: list[str] = ["/tmp/agentgate"]

    # Browser / Playwright
    PLAYWRIGHT_HEADLESS: bool = True
    PLAYWRIGHT_MAX_ELEMENTS: int = Field(default=50, ge=10, le=200)
    BROWSER_MAX_CONCURRENT_SESSIONS: int = Field(default=10, ge=1, le=100)

    # Data & Storage
    # AUDIT_BACKEND: "postgres" (default -- action-sourced, writes to
    # audit_logs via migration 0001) or "jsonl" (Sprint 1/2 fallback, no DB
    # required -- kept as an escape hatch, e.g. local dev without a DB, or
    # rollback if the postgres path breaks).
    AUDIT_BACKEND: Literal["jsonl", "postgres"] = "postgres"
    AUDIT_LOG_PATH: str = "artifacts/audit/events.jsonl"
    TRACE_LOG_PATH: str = "artifacts/traces/actions.jsonl"
    # Pending-approval queue (see pending-approval-design.md). 30 minutes was
    # flagged in the design as a placeholder -- made configurable here rather
    # than hardcoded so it can be tuned (e.g. per-environment) without a code
    # change. Per-risk_level TTLs (design doc open question 2) are not yet
    # implemented; this is a single global default for now.
    APPROVAL_TTL_MINUTES: int = Field(default=30, ge=1, le=1440)
    # Pending-clarification queue (ASK_USER decision path). Shorter default
    # than APPROVAL_TTL_MINUTES: the end user is presumably live in the
    # loop that just proposed the action, unlike a reviewer who may pick
    # up an approval later.
    ASK_USER_TTL_MINUTES: int = Field(default=10, ge=1, le=1440)
    # confidence below this on an ActionRequest routes to ASK_USER instead
    # of falling through to the risk_hint / ALLOW check in decide().
    ASK_USER_CONFIDENCE_THRESHOLD: float = Field(default=0.5, ge=0, le=1)
    AUDIT_RETENTION_DAYS: int = Field(default=7, ge=1, le=365)
    TRACE_RETENTION_DAYS: int = Field(default=7, ge=1, le=365)
    SCREENSHOT_RETENTION_DAYS: int = Field(default=7, ge=0, le=365)
    DATA_DIR: str = "./data"

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_db_url(cls, v: str) -> str:
        if v.startswith("sqlite"):
            return v
        if not v.startswith("postgresql"):
            raise ValueError(
                f"DATABASE_URL must start with 'postgresql' or 'sqlite', got: {v[:30]}..."
            )
        return v

    @field_validator("ALLOWED_FILESYSTEM_PATHS", mode="before")
    @classmethod
    def validate_paths(cls, v: list[str]) -> list[str]:
        if isinstance(v, str):
            v = [p.strip() for p in v.split(",") if p.strip()]
        for path in v:
            if not os.path.isabs(path):
                raise ValueError(f"ALLOWED_FILESYSTEM_PATHS must be absolute: {path}")
        return v


def _resolve_settings_class(env: str) -> type[Settings]:
    """Return the correct Settings subclass for the given environment."""
    if env == "development":
        from app.config.development import DevelopmentSettings

        return DevelopmentSettings
    if env == "staging":
        from app.config.staging import StagingSettings

        return StagingSettings
    if env == "production":
        from app.config.production import ProductionSettings

        return ProductionSettings
    return Settings


@lru_cache()
def get_settings() -> Settings:
    """Return the Settings instance for the active environment.

    - Reads ``APP_ENV`` from the environment to select the correct subclass.
    - Subclasses are lazily imported from ``app/config/{development,staging,production}.py``.
    - Environment variables from ``.env`` (or system env) are layered on top of defaults.

    In tests, call ``get_settings.cache_clear()`` after changing ``APP_ENV`` via
    ``monkeypatch`` to force re-instantiation.
    """
    raw_env = os.environ.get("APP_ENV", "development")
    settings_cls = _resolve_settings_class(raw_env)
    return settings_cls()
