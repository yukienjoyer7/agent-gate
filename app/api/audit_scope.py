"""Filter durable audit events to the request's browser/legacy owner scope."""

from app.api.session_context import OwnerContext
from app.core.audit_schema import AuditEvent


def event_belongs_to_owner(event: AuditEvent, owner: OwnerContext) -> bool:
    request_json = event.request_json or {}
    if owner.session_id is not None:
        return request_json.get("session_id") == owner.session_id
    if request_json.get("session_id") is not None:
        return False
    return str(request_json.get("owner_id") or "default") == owner.owner_id
