import logging

from fastapi import FastAPI

from app.api.v1 import actions, approvals, ask_user, audits, benchmark, health, runs
from app.config.logging import configure_logging
from app.config.settings import get_settings

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="AgentGate",
        version="0.1.0",
        description="Guarded agent execution platform (MVP)",
    )

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(runs.router, prefix="/api/v1")
    app.include_router(actions.router, prefix="/api/v1")
    app.include_router(audits.router, prefix="/api/v1")
    app.include_router(approvals.router, prefix="/api/v1")
    app.include_router(ask_user.router, prefix="/api/v1")
    app.include_router(benchmark.router, prefix="/api/v1")

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": "agentgate",
            "env": settings.APP_ENV,
            "version": "0.1.0",
        }

    @app.on_event("startup")
    async def on_startup() -> None:
        logger.info(
            "AgentGate starting",
            extra={
                "env": settings.APP_ENV,
                "debug": settings.DEBUG,
                "log_level": settings.LOG_LEVEL,
            },
        )

    return app


app = create_app()
