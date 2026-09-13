"""Non-secret local configuration; no implicit project .env loading."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from platformdirs import user_config_path, user_data_path
from pydantic import BaseModel, ConfigDict, Field, field_validator


@dataclass(frozen=True)
class LocalPaths:
    config: Path
    data: Path

    @classmethod
    def resolve(cls, config: Path | None = None) -> LocalPaths:
        config_dir = Path(os.environ.get("AGENTGATE_CONFIG_DIR") or user_config_path("agentgate"))
        data_dir = Path(os.environ.get("AGENTGATE_DATA_DIR") or user_data_path("agentgate"))
        return cls(config or config_dir / "config.toml", data_dir.absolute())

    @property
    def database(self) -> Path:
        return self.data / "state.sqlite3"


class LocalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_type: Literal["openai", "anthropic", "gemini"] = "openai"
    llm_url: str = "https://openrouter.ai/api/v1/chat/completions"
    llm_model: str = "openrouter/free"
    timezone: str = "Asia/Jakarta"
    workspace: str = ""
    credential_store: Literal["keyring", "env", "session"] = "keyring"
    browser_enabled: bool = False
    google_client_id: str = ""
    stripe_success_url: str = ""
    stripe_cancel_url: str = ""
    stripe_price_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown timezone") from exc
        return value

    @field_validator("llm_url")
    @classmethod
    def valid_llm_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("LLM URL cannot contain credentials, query parameters, or fragments")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("LLM URL requires HTTPS, except for a loopback local model")
        if not url.hostname:
            raise ValueError("LLM URL requires a hostname")
        return value

    @field_validator("workspace")
    @classmethod
    def absolute_workspace(cls, value: str) -> str:
        if value and not Path(value).is_absolute():
            raise ValueError("Workspace must be an absolute path")
        return value


def load_config(path: Path) -> LocalConfig:
    if not path.exists():
        raise ValueError("Local configuration is missing. Run 'agentgate init' first")
    with path.open("rb") as stream:
        return LocalConfig.model_validate(tomllib.load(stream))


def save_config(path: Path, config: LocalConfig) -> None:
    # An explicit project config can live in a shared workspace. Preserve
    # permissions on its existing parent directory.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Configuration must not be a symlink")
    values = config.model_dump(exclude={"stripe_price_map"})
    lines = []
    for key, value in values.items():
        literal = (
            str(value).lower() if isinstance(value, bool) else json.dumps(value, ensure_ascii=False)
        )
        lines.append(f"{key} = {literal}")
    lines.extend(["", "[stripe_price_map]"])
    lines.extend(
        f"{json.dumps(key)} = {json.dumps(value)}" for key, value in config.stripe_price_map.items()
    )
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
