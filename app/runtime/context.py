"""Task-scoped dependencies while existing domain services are extracted.

Context variables propagate into run tasks without changing server defaults or
sharing credentials between independently composed runtimes.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.config.settings import Settings
    from app.core.schemas import ActionRequest, AuditEvent, DecisionResponse, ExecutionResult
    from app.domains.oauth.repository import StoredToken


class AuditStore(Protocol):
    async def write(
        self,
        request: ActionRequest,
        decision: DecisionResponse,
        execution: ExecutionResult,
        latency: dict[str, int] | None = None,
    ) -> AuditEvent: ...


class Router(Protocol):
    async def route(
        self, action: ActionRequest, decision: DecisionResponse, **kwargs: Any
    ) -> ExecutionResult: ...


class TokenStore(Protocol):
    async def get(self, provider: str) -> StoredToken | None: ...

    async def save(
        self,
        provider: str,
        access_token: str,
        refresh_token: str | None,
        expires_at: datetime | None,
        scope: str | None,
    ) -> StoredToken: ...


class ExecutionJournal(Protocol):
    def begin(
        self, request: ActionRequest, operation_key: str | None = None
    ) -> tuple[str, ExecutionResult | None]: ...
    def finish(self, key: str, request: ActionRequest, result: ExecutionResult) -> None: ...
    def unknown(self, key: str) -> None: ...


@dataclass(frozen=True)
class RuntimeDependencies:
    settings: Settings
    audit: AuditStore
    router: Router
    tokens: TokenStore
    traces: Any
    intents: ExecutionJournal | None = None
    sanitize_messages: Callable[[Any], Any] | None = None


_current: ContextVar[RuntimeDependencies | None] = ContextVar("agentgate_runtime", default=None)


def current_runtime() -> RuntimeDependencies | None:
    return _current.get()


@contextmanager
def bind_runtime(dependencies: RuntimeDependencies):
    token = _current.set(dependencies)
    try:
        yield dependencies
    finally:
        _current.reset(token)
