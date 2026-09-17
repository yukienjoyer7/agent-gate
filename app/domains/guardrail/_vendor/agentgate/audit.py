"""Host audit boundary replacing upstream's mandatory PostgreSQL factory.

The application supplies a durable audit store to DecisionEngine. Deliberately
provide no default store: constructing an unaudited engine must fail.
"""

STAGE_ACTION = "action"


class AuditUnavailable(RuntimeError):
    """A guardrail decision could not be durably recorded."""


def build_audit_store():
    raise AuditUnavailable("The embedded guardrail requires a host audit store")
