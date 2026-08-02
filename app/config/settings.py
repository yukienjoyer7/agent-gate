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
    # Static tokens are a fallback used only when no OAuth token has been
    # stored yet for that provider (see app/domains/oauth/service.py).
    GITHUB_TOKEN: str = ""
    GMAIL_ACCESS_TOKEN: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    XENDIT_API_KEY: str = ""

    # OAuth apps (GitHub OAuth App / Google Cloud OAuth client)
    GITHUB_OAUTH_CLIENT_ID: str = ""
    GITHUB_OAUTH_CLIENT_SECRET: str = ""
    GITHUB_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/oauth/github/callback"
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/oauth/gmail/callback"

    # LLM (OpenRouter)
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "openrouter/free"
    OPENROUTER_TIMEOUT: float = 60.0
    # Function calling: let the planner inspect a page's accessibility tree
    # (get_accessibility_tree) before emitting element interaction steps.
    LLM_TOOLS_ENABLED: bool = True
    # Free models often explore several URLs (open page, docs, subpages) via
    # tool calls before answering; keep the cap generous but bounded.
    LLM_MAX_TOOL_ITERATIONS: int = Field(default=5, ge=1, le=10)

    # Filesystem
    LOCAL_FILE_ROOT: str = "demo_data"
    ALLOWED_FILESYSTEM_PATHS: list[str] = ["/tmp/agentgate"]

    # Browser / Playwright
    PLAYWRIGHT_HEADLESS: bool = True
    PLAYWRIGHT_MAX_ELEMENTS: int = Field(default=50, ge=10, le=200)
    BROWSER_MAX_CONCURRENT_SESSIONS: int = Field(default=10, ge=1, le=100)
    # SPA pages (e.g. YouTube) render interactive headers slightly after
    # domcontentloaded; both the planner tool (get_accessibility_tree) and the
    # executor settle this long so the snapshots they see stay consistent.
    BROWSER_SETTLE_MS: int = Field(default=2000, ge=0, le=10000)

    # Data & Storage
    # AUDIT_BACKEND: "postgres" (default -- action-sourced, writes to
    # audit_logs via migration 0001) or "jsonl" (Sprint 1/2 fallback, no DB
    # required -- kept as an escape hatch, e.g. local dev without a DB, or
    # rollback if the postgres path breaks).
    AUDIT_BACKEND: Literal["jsonl", "postgres"] = "postgres"
    AUDIT_LOG_PATH: str = "artifacts/audit/events.jsonl"
    TRACE_LOG_PATH: str = "artifacts/traces/actions.jsonl"
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


@lru_cache
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
