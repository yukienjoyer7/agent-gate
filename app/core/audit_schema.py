from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.action_schema import SCHEMA_VERSION, ExecutionStatus, new_id, utc_now


class AuditEvent(BaseModel):
    schema_version: Literal["0.1"] = SCHEMA_VERSION
    audit_id: str = Field(default_factory=lambda: new_id("aud"))
    run_id: str
    action_id: str
    request_json: dict[str, Any]
    decision_json: dict[str, Any]
    execution_json: dict[str, Any]
    execution_status: ExecutionStatus
    error_type: str | None = None
    policy_version: str = "policy-0.1"
    detector_version: str = "detector-0.1"
    latency: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ActionTrace(BaseModel):
    schema_version: Literal["0.1"] = SCHEMA_VERSION
    run_id: str
    action_id: str
    user_goal: str = ""
    raw_tool_call: dict[str, Any]
    action_request: dict[str, Any]
    decision: dict[str, Any]
    execution: dict[str, Any]
    audit: dict[str, Any]
    latency: dict[str, int]
    final_status: ExecutionStatus
    created_at: datetime = Field(default_factory=utc_now)
