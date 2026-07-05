from fastapi import APIRouter

from app.domains.audit.repositories import AuditRepository

router = APIRouter(tags=["audits"])


@router.get("/audits")
async def list_audits(run_id: str | None = None) -> list[dict]:
    repo = AuditRepository()
    events = repo.by_run(run_id) if run_id else repo.list()
    return [event.model_dump(mode="json") for event in events]


@router.get("/audits/latest")
async def latest_audit() -> dict:
    event = AuditRepository().latest()
    return event.model_dump(mode="json") if event else {}
