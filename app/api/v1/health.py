from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = Field(default="ok", description="Service operational status", examples=["ok"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health Check",
    description="Return operational health status of the AgentGate service.",
)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")
