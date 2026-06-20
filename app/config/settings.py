from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment / .env files."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "postgresql+psycopg://agentgate:agentgate@localhost:5432/agentgate"

    # Queue
    REDIS_URL: str = "redis://localhost:6379/0"

    # LLM providers
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""

    # Connectors
    GITHUB_TOKEN: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    STRIPE_API_KEY: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
