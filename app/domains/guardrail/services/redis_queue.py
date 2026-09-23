"""Redis-backed FIFO gate for expensive guardrail model evaluations.

The queue deliberately stores only opaque tickets.  Action payloads, prompts,
and detector results stay in the AgentGate process and are never written to
Redis.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class GuardrailQueueError(RuntimeError):
    """Base error for failures before an evaluation can enter the queue."""

    category = "queue_unavailable"


class GuardrailQueueUnavailable(GuardrailQueueError):
    """Redis could not be reached or returned an unusable response."""


class GuardrailQueueTimeout(GuardrailQueueError):
    """The ticket did not reach the front of the queue before its deadline."""

    category = "queue_timeout"


# Remove abandoned tickets from the front, then atomically grant the lease to
# the current FIFO head.  Ticket liveness keys have a short TTL, so a crashed
# API process cannot block the queue indefinitely.
_ACQUIRE_SCRIPT = """
while true do
  local head = redis.call('LINDEX', KEYS[1], 0)
  if not head then
    return 0
  end
  if redis.call('EXISTS', ARGV[3] .. head) == 0 then
    redis.call('LPOP', KEYS[1])
  else
    break
  end
end
local head = redis.call('LINDEX', KEYS[1], 0)
if head ~= ARGV[1] then
  return 0
end
if redis.call('SET', KEYS[2], ARGV[1], 'NX', 'PX', ARGV[2]) then
  return 1
end
if redis.call('GET', KEYS[2]) == ARGV[1] then
  return 1
end
return 0
"""


_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[2]) == ARGV[1] then
  redis.call('DEL', KEYS[2])
end
if redis.call('LINDEX', KEYS[1], 0) == ARGV[1] then
  redis.call('LPOP', KEYS[1])
else
  redis.call('LREM', KEYS[1], 1, ARGV[1])
end
redis.call('DEL', ARGV[2])
return 1
"""


def _connect(redis_url: str, socket_timeout: float) -> Any:
    # Redis remains a server-only optional dependency.  Import it only when the
    # queue is enabled so the base CLI package continues to work without it.
    from redis import Redis

    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=socket_timeout,
        socket_timeout=socket_timeout,
        health_check_interval=30,
    )


@contextmanager
def guardrail_evaluation_slot(
    *,
    enabled: bool,
    redis_url: str,
    queue_name: str,
    wait_timeout: float,
    lease_seconds: float,
    poll_interval: float = 0.25,
    socket_timeout: float = 2.0,
    client_factory: Callable[[str, float], Any] = _connect,
) -> Iterator[None]:
    """Yield once this caller owns the single FIFO evaluation slot.

    The lease is intentionally longer than the detector HTTP timeout.  The
    blocking detector call runs in ``asyncio.to_thread`` in API usage, so even
    cancellation of the parent coroutine lets this context finish and release
    the lease only after the underlying model request has actually stopped.
    """

    if not enabled:
        yield
        return

    ticket = uuid4().hex
    waiting_key = f"{queue_name}:waiting"
    active_key = f"{queue_name}:active"
    alive_prefix = f"{queue_name}:ticket:"
    alive_key = alive_prefix + ticket
    alive_ttl = max(10, int(poll_interval * 20))
    lease_ms = max(1, int(lease_seconds * 1000))
    deadline = time.monotonic() + wait_timeout
    client: Any | None = None
    acquired = False

    def cleanup() -> None:
        if client is None:
            return
        try:
            if acquired:
                client.eval(
                    _RELEASE_SCRIPT,
                    2,
                    waiting_key,
                    active_key,
                    ticket,
                    alive_key,
                )
                logger.info("guardrail evaluation finished ticket=%s", ticket[:8])
            else:
                client.lrem(waiting_key, 1, ticket)
                client.delete(alive_key)
        except Exception:
            logger.exception("could not clean up guardrail Redis queue ticket")
        finally:
            try:
                client.close()
            except Exception:
                logger.debug("could not close guardrail Redis client", exc_info=True)

    try:
        client = client_factory(redis_url, socket_timeout)
        client.set(alive_key, "1", ex=alive_ttl)
        client.rpush(waiting_key, ticket)
        logger.info("guardrail evaluation queued ticket=%s", ticket[:8])

        while True:
            client.expire(alive_key, alive_ttl)
            acquired = bool(
                client.eval(
                    _ACQUIRE_SCRIPT,
                    2,
                    waiting_key,
                    active_key,
                    ticket,
                    lease_ms,
                    alive_prefix,
                )
            )
            if acquired:
                logger.info("guardrail evaluation started ticket=%s", ticket[:8])
                break
            if time.monotonic() >= deadline:
                raise GuardrailQueueTimeout(
                    f"Guardrail evaluation queue wait exceeded {wait_timeout:g} seconds"
                )
            time.sleep(poll_interval)

    except GuardrailQueueError:
        cleanup()
        raise
    except Exception as exc:  # Redis exceptions are optional-dependency-specific.
        cleanup()
        raise GuardrailQueueUnavailable("Guardrail Redis queue is unavailable") from exc

    # Exceptions raised by the guarded evaluation must propagate unchanged;
    # they are not Redis failures.  Cleanup is best-effort and the lease TTL is
    # the final crash-safety mechanism.
    try:
        yield
    finally:
        cleanup()
