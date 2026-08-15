from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # LLM provider (LLM_TYPE: "openai" = OpenAI-compatible chat completions,
    # "anthropic" = Anthropic Messages API — the client translates the shared
    # payload/response shape between the two).
    LLM_API_KEY: str = ""
    LLM_TYPE: Literal["openai", "anthropic"] = "openai"
    LLM_URL: str = "https://openrouter.ai/api/v1/chat/completions"
    LLM_MODEL: str = "openrouter/free"
    LLM_TIMEOUT: float = 60.0
    # Required by the Anthropic Messages API (ignored for openai type).
    LLM_MAX_TOKENS: int = Field(default=4096, ge=256, le=128000)

    # Planner validation rules (single source of truth — overridable via env
    # as comma-separated values, e.g.
    # ALLOWED_ACTION_TYPES=BROWSER_OPEN,BROWSER_CLICK).
    # ``NoDecode`` keeps the raw env string (JSON decoding is skipped) so the
    # comma-separated validator below can split it.
    ALLOWED_ACTION_TYPES: Annotated[list[str], NoDecode] = [
        "BROWSER_OPEN",
        "BROWSER_CLICK",
        "BROWSER_TYPE",
        "BROWSER_SCROLL",
        "BROWSER_SCREENSHOT",
        "BROWSER_SUBMIT",
        "BROWSER_SELECT",
        "API_CALL",
        "FILE_READ",
    ]
    ALLOWED_TARGET_SYSTEMS: Annotated[list[str], NoDecode] = [
        "browser",
        "gmail",
        "github",
        "local_file",
        "stripe",
    ]
    ALLOWED_DOMAINS: Annotated[list[str], NoDecode] = [
        "browser",
        "productivity",
        "code_protection",
        "booking",
        "filesystem",
    ]
    ALLOWED_RISK_HINTS: Annotated[list[str], NoDecode] = [
        "unknown",
        "external_send",
        "file_read",
        "destructive",
        "unauthorized",
        "data_exfiltration",
        "payment",
        "refund",
        "bulk_action",
    ]
    INTERACTIVE_BROWSER_ACTIONS: Annotated[list[str], NoDecode] = [
        "BROWSER_CLICK",
        "BROWSER_TYPE",
        "BROWSER_SCROLL",
        "BROWSER_SCREENSHOT",
        "BROWSER_SUBMIT",
        "BROWSER_SELECT",
    ]
    # Domain fallbacks applied when the LLM omits ``domain`` so it cannot
    # silently downgrade guardrail risk decisions.
    DOMAIN_BY_TARGET_SYSTEM: dict[str, str] = {
        "gmail": "productivity",
        "github": "code_protection",
        "local_file": "filesystem",
        "stripe": "booking",
        "browser": "browser",
    }
    # Function calling: let the planner inspect a page's accessibility tree
    # (get_accessibility_tree) before emitting element interaction steps.
    LLM_TOOLS_ENABLED: bool = True
    # Free models often explore several URLs (open page, docs, subpages) via
    # tool calls before answering; keep the cap generous but bounded.
    LLM_MAX_TOOL_ITERATIONS: int = Field(default=5, ge=1, le=10)

    # Guardrail (dedicated LLM model)
    # When enabled, the guardrail runs a second-opinion LLM review on top of
    # the deterministic rule engine for every non-BLOCK decision. A rule-based
    # BLOCK is always final. Defaults to OFF so behaviour is unchanged unless
    # explicitly opted in (the rules are the safe fallback either way).
    GUARDRAIL_LLM_ENABLED: bool = False
    # Model for the guardrail judge; empty -> falls back to LLM_MODEL.
    GUARDRAIL_MODEL: str = ""
    # Which risk_hints the deterministic rule engine treats as BLOCK vs
    # NEED_APPROVAL (subsets of ALLOWED_RISK_HINTS; overridable via env).
    GUARDRAIL_BLOCK_HINTS: Annotated[list[str], NoDecode] = [
        "destructive",
        "unauthorized",
        "data_exfiltration",
    ]
    GUARDRAIL_NEED_APPROVAL_HINTS: Annotated[list[str], NoDecode] = [
        "external_send",
        "payment",
        "bulk_action",
        "refund",
    ]
    # ASK_USER risk_hints: too little information to decide → the guardrail
    # asks the user for clarification before any execution.
    GUARDRAIL_ASK_USER_HINTS: Annotated[list[str], NoDecode] = [
        "ambiguous_target",
        "missing_target",
        "clarification_needed",
    ]
    # Default domain when a step/action carries none (falls back to this
    # instead of the raw string "productivity" being repeated in the schema,
    # builder and guardrail).
    DEFAULT_DOMAIN: str = "productivity"

    # Reactive agent loop
    # Hard caps: total steps per run (initial plan + replanned steps) and the
    # number of replan (LLM) calls made after failures / plan exhaustion.
    AGENT_MAX_STEPS: int = Field(default=12, ge=1, le=100)
    AGENT_MAX_REPLAN: int = Field(default=4, ge=0, le=20)
    # How long a step may pause waiting for an approve/decline/input response
    # before the run is marked FAILED (seconds).
    AGENT_WAIT_RESPONSE_TIMEOUT_SEC: float = Field(default=600.0, ge=5.0, le=3600.0)
    # Overall run timeout: a hung browser/connector call must not keep the
    # SSE stream open forever — the run is cancelled and an error emitted.
    AGENT_RUN_TIMEOUT_SEC: float = Field(default=900.0, ge=30.0, le=7200.0)
    # Heartbeat interval for the SSE stream while a run is paused (seconds).
    SSE_HEARTBEAT_SEC: float = Field(default=15.0, ge=1.0, le=120.0)
    # Cap on retained in-memory run sessions (oldest evicted first).
    RUN_REGISTRY_MAX_SESSIONS: int = Field(default=500, ge=1, le=10000)

    # Filesystem
    LOCAL_FILE_ROOT: str = "demo_data"
    ALLOWED_FILESYSTEM_PATHS: list[str] = ["/tmp/agentgate"]

    # Browser / Playwright
    PLAYWRIGHT_HEADLESS: bool = True
    PLAYWRIGHT_MAX_ELEMENTS: int = Field(default=50, ge=10, le=500)
    BROWSER_MAX_CONCURRENT_SESSIONS: int = Field(default=10, ge=1, le=100)
    # Default navigation wait condition + timeout for page.goto() calls (the
    # executor, the accessibility-tree tool and the demo script all share it).
    BROWSER_WAIT_UNTIL: Literal["commit", "domcontentloaded", "load", "networkidle"] = (
        "domcontentloaded"
    )
    BROWSER_TIMEOUT_MS: int = Field(default=15_000, ge=1_000, le=60_000)
    # User agent for Playwright pages; empty -> the Chrome-124 default in
    # app/domains/browser/browser_profile.py (single source, shared by every
    # browser-launching module).
    BROWSER_USER_AGENT: str = ""
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

    @field_validator(
        "ALLOWED_ACTION_TYPES",
        "ALLOWED_TARGET_SYSTEMS",
        "ALLOWED_DOMAINS",
        "ALLOWED_RISK_HINTS",
        "INTERACTIVE_BROWSER_ACTIONS",
        "GUARDRAIL_BLOCK_HINTS",
        "GUARDRAIL_NEED_APPROVAL_HINTS",
        "GUARDRAIL_ASK_USER_HINTS",
        mode="before",
    )
    @classmethod
    def validate_csv_lists(cls, v):
        """Accept comma-separated (or JSON array) env values for list settings."""
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith("["):
                import json

                parsed = json.loads(stripped)
                return parsed if isinstance(parsed, list) else [str(parsed)]
            v = [item.strip() for item in stripped.split(",") if item.strip()]
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
