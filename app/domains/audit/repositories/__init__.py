from app.config.settings import get_settings
from app.domains.audit.repositories.audit_repository import AuditRepository
from app.runtime.context import current_runtime

__all__ = ["AuditRepository", "AuditRepositoryDB", "get_audit_repository"]


def __getattr__(name: str):
    if name == "AuditRepositoryDB":
        from app.domains.audit.repositories.audit_repository_db import AuditRepositoryDB

        return AuditRepositoryDB
    raise AttributeError(name)


def get_audit_repository():
    """
    Returns the configured audit repository, chosen via
    settings.AUDIT_BACKEND ("jsonl" | "postgres"). The settings default is
    "postgres" (action-sourced, writes to audit_logs via migration 0001);
    "jsonl" is the no-DB escape hatch for local dev / rollback. The value
    lives in one place — ``app.config.settings`` — and can be overridden per
    environment via env, e.g. ``AUDIT_BACKEND=jsonl`` in .env.

    Both repositories expose the same async interface (write, latest, list,
    by_run, by_action), mirroring the same write-once-per-action behavior --
    so call sites only need to swap AuditRepository() -> get_audit_repository()
    and await the result; nothing else changes regardless of backend.
    """
    runtime = current_runtime()
    if runtime is not None:
        return runtime.audit
    backend = get_settings().AUDIT_BACKEND
    if backend == "postgres":
        from app.domains.audit.repositories.audit_repository_db import AuditRepositoryDB

        return AuditRepositoryDB()
    return AuditRepository()
