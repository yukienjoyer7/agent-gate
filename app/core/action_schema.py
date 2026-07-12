from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.core.browser_schema import BrowserElement


SCHEMA_VERSION = "0.1"


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class Decision(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    NEED_APPROVAL = "NEED_APPROVAL"
    SANITIZE = "SANITIZE"
    ASK_USER = "ASK_USER"


class ExecutionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    PENDING_APPROVAL = "PENDING_APPROVAL"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ActionRequest(BaseModel):
    schema_version: Literal["0.1"] = SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: new_id("run"))
    action_id: str = Field(default_factory=lambda: new_id("act"))
    source: str = "cli"
    domain: str = "productivity"
    action_type: str
    target_system: str
    target: str | dict[str, Any]
    content_context: str = ""
    payload_summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict, exclude=True)
    browser_element: BrowserElement | None = None
    risk_hint: str = "unknown"
    rollback_available: bool = False
    confidence: float = Field(default=1.0, ge=0, le=1)
    created_at: datetime = Field(default_factory=utc_now)


class DecisionResponse(BaseModel):
    schema_version: Literal["0.1"] = SCHEMA_VERSION
    run_id: str
    action_id: str
    decision: Decision
    risk_level: RiskLevel = RiskLevel.LOW
    risk_score: float = Field(default=0, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    triggered_policies: list[str] = Field(default_factory=list)
    sensitive_entities: list[str] = Field(default_factory=list)
    sanitized_payload: dict[str, Any] | None = None
    next_step: str = "execute"
    latency_ms: int = 0
    created_at: datetime = Field(default_factory=utc_now)
