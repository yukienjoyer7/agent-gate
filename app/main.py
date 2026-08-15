import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import actions, approvals, audits, benchmark, chat, health, oauth, runs
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
