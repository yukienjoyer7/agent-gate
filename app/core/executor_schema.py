from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.action_schema import SCHEMA_VERSION, ExecutionStatus, utc_now


class ExecutionResult(BaseModel):
    schema_version: Literal["0.1"] = SCHEMA_VERSION
    run_id: str
    action_id: str
    executor: str
    status: ExecutionStatus
    result_summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    latency_ms: int = 0
    created_at: datetime = Field(default_factory=utc_now)
