from typing import Union

from app.config.settings import get_settings
from app.domains.audit.repositories.audit_repository import AuditRepository
from app.domains.audit.repositories.audit_repository_db import AuditRepositoryDB

__all__ = ["AuditRepository", "AuditRepositoryDB", "get_audit_repository"]

AnyAuditRepository = Union[AuditRepository, AuditRepositoryDB]


def get_audit_repository() -> AnyAuditRepository:
    """
    Returns the configured audit repository, chosen via
    settings.AUDIT_BACKEND ("jsonl" | "postgres"). Default is "jsonl" --
    unchanged behavior from Sprint 1/2 -- until "postgres" is explicitly
    opted into per environment.

    Both repositories expose the same async interface (write, latest, list,
    by_run, by_action), mirroring the same write-once-per-action behavior --
    so call sites only need to swap AuditRepository() -> get_audit_repository()
    and await the result; nothing else changes regardless of backend.
    """
    backend = get_settings().AUDIT_BACKEND
    if backend == "postgres":
        return AuditRepositoryDB()
    return AuditRepository()

