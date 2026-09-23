from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

import pytest

from app.domains.guardrail.services.redis_queue import guardrail_evaluation_slot


class FakeRedis:
    """Small thread-safe fake for the exact commands used by the FIFO gate."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = defaultdict(list)

    def set(self, key: str, value: str, **kwargs: Any) -> bool:
        with self.lock:
            if kwargs.get("nx") and key in self.values:
                return False
            self.values[key] = value
            return True

    def rpush(self, key: str, value: str) -> None:
        with self.lock:
            self.lists[key].append(value)

    def expire(self, key: str, seconds: int) -> bool:
        return key in self.values

    def eval(self, script: str, key_count: int, *args: Any) -> int:
        waiting_key, active_key = args[:2]
        ticket = args[2]
        with self.lock:
            if "while true do" in script:
                alive_prefix = args[4]
                while self.lists[waiting_key]:
                    head = self.lists[waiting_key][0]
                    if alive_prefix + head in self.values:
                        break
                    self.lists[waiting_key].pop(0)
                if not self.lists[waiting_key] or self.lists[waiting_key][0] != ticket:
                    return 0
                if active_key not in self.values:
                    self.values[active_key] = ticket
                    return 1
                return int(self.values[active_key] == ticket)

            alive_key = args[3]
            if self.values.get(active_key) == ticket:
                self.values.pop(active_key, None)
            if ticket in self.lists[waiting_key]:
                self.lists[waiting_key].remove(ticket)
            self.values.pop(alive_key, None)
            return 1

    def lrem(self, key: str, count: int, value: str) -> None:
        with self.lock:
            if value in self.lists[key]:
                self.lists[key].remove(value)

    def delete(self, key: str) -> None:
        with self.lock:
            self.values.pop(key, None)

    def close(self) -> None:
        pass


def slot(fake: FakeRedis):
    return guardrail_evaluation_slot(
        enabled=True,
        redis_url="redis://test",
        queue_name="test:guardrail",
        wait_timeout=2,
        lease_seconds=2,
        poll_interval=0.01,
        client_factory=lambda url, timeout: fake,
    )


def test_queue_allows_only_one_evaluation_in_fifo_order() -> None:
    fake = FakeRedis()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    order: list[str] = []

    def first() -> None:
        with slot(fake):
            order.append("first")
            first_entered.set()
            assert release_first.wait(2)

    def second() -> None:
        with slot(fake):
            order.append("second")
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    assert first_entered.wait(1)
    second_thread.start()
    time.sleep(0.05)
    assert not second_entered.is_set()
    release_first.set()
    first_thread.join(1)
    second_thread.join(1)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert order == ["first", "second"]


def test_evaluation_error_is_not_relabelled_as_redis_failure() -> None:
    fake = FakeRedis()
    with pytest.raises(ValueError, match="detector broke"), slot(fake):
        raise ValueError("detector broke")


def test_disabled_queue_does_not_connect() -> None:
    def forbidden(url: str, timeout: float):
        pytest.fail("disabled queue must not connect to Redis")

    with guardrail_evaluation_slot(
        enabled=False,
        redis_url="redis://test",
        queue_name="test:guardrail",
        wait_timeout=1,
        lease_seconds=1,
        client_factory=forbidden,
    ):
        pass
