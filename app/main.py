from fastapi import FastAPI

from app.api.v1 import actions, approvals, audits, benchmark, health, runs
from app.config.logging import configure_logging
from app.config.settings import get_settings


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
    app.include_router(benchmark.router, prefix="/api/v1")

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": "agentgate", "env": settings.APP_ENV}

    return app


app = create_app()
