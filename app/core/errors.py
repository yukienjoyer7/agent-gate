from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ConnectorErrorCode(StrEnum):
    AUTH = "AUTH"
    PERMISSION = "PERMISSION"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    VALIDATION = "VALIDATION"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"


class ConnectorError(BaseModel):
    code: ConnectorErrorCode
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def validation(cls, message: str) -> "ConnectorError":
        return cls(code=ConnectorErrorCode.VALIDATION, message=message)
