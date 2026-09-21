from contextlib import asynccontextmanager
import asyncio
import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.api.v1 import (
    actions,
    approvals,
    audits,
    benchmark,
    chat,
    health,
    oauth,
    runs,
    sessions,
    stripe,
    telegram,
)
from app.config.logging import configure_logging
from app.config.settings import get_settings

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

logger = logging.getLogger(__name__)


class RootResponse(BaseModel):
    service: str = Field(default="agentgate", description="Service identifier", examples=["agentgate"])
    env: str = Field(..., description="Active runtime environment", examples=["development"])
    version: str = Field(default="0.1.0", description="Application semantic version", examples=["0.1.0"])


TAGS_METADATA = [
    {"name": "health", "description": "Service operational and health check endpoints."},
    {"name": "runs", "description": "Agent execution runs and historical action queries."},
    {"name": "actions", "description": "Guarded action proposal execution, browser prototyping, and action detail inspection."},
    {"name": "audits", "description": "Audit event log queries and latest event retrieval."},
    {"name": "approvals", "description": "Human-in-the-loop pending approval queue."},
    {"name": "benchmark", "description": "Execution latency and performance metrics."},
    {"name": "chat", "description": "Reactive agent chat planning, SSE streaming execution, state inspection, and step interaction."},
    {"name": "oauth", "description": "OAuth 2.0 connection management, authorization redirects, and callbacks."},
    {"name": "stripe", "description": "Stripe payment and checkout webhook event processing."},
    {"name": "telegram", "description": "Telegram Bot webhook and inbound update processing."},
    {"name": "sessions", "description": "Browser demo session lifecycle and isolated run state."},
    {"name": "system", "description": "System and service information."},
]


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "AgentGate starting",
            extra={
                "env": settings.APP_ENV,
                "debug": settings.DEBUG,
                "log_level": settings.LOG_LEVEL,
            },
        )
        sweeper = asyncio.create_task(_browser_session_sweeper())
        try:
            yield
        finally:
            sweeper.cancel()
            try:
                await sweeper
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="AgentGate",
        version="0.1.0",
        description="Guarded agent execution platform (MVP) — provides safety guardrails, execution routing, and durable audit logs.",
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )

    # Allow browser-based demo clients (e.g. the fe/ demo page) to call the API
    # from any origin. Credentials are never used, so "*" is safe.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(runs.router, prefix="/api/v1")
    app.include_router(actions.router, prefix="/api/v1")
    app.include_router(audits.router, prefix="/api/v1")
    app.include_router(approvals.router, prefix="/api/v1")
    app.include_router(benchmark.router, prefix="/api/v1")
    app.include_router(chat.router, prefix="/api/v1")
    app.include_router(oauth.router, prefix="/api/v1")
    app.include_router(stripe.router, prefix="/api/v1")
    app.include_router(telegram.router, prefix="/api/v1")
    app.include_router(sessions.router, prefix="/api/v1")

    @app.get(
        "/",
        response_model=RootResponse,
        tags=["system"],
        summary="Service Information",
        description="Return basic service metadata, version, and running environment.",
    )
    async def root() -> RootResponse:
        return RootResponse(
            service="agentgate",
            env=settings.APP_ENV,
            version="0.1.0",
        )

    return app


async def _browser_session_sweeper() -> None:
    """Expire abandoned browser sessions and their ephemeral resources."""
    from app.domains.sessions.service import expire_idle_browser_sessions

    interval = get_settings().BROWSER_SESSION_SWEEP_INTERVAL_SEC
    while True:
        await asyncio.sleep(interval)
        try:
            await expire_idle_browser_sessions()
        except Exception:  # noqa: BLE001 - keep future sweeps alive
            logger.exception("browser session cleanup sweep failed")

app = create_app()
