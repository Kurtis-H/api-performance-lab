"""A small TTL cache, with and without stampede protection.

Without protection, a hot key expiring means every request that arrives before
the first recompute finishes runs the expensive query too. With protection
("single flight"), one request recomputes while the others wait for its result.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    def __init__(
        self,
        ttl_seconds: float,
        single_flight: bool,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl_seconds
        self.single_flight = single_flight
        self._clock = clock
        self._values: dict[Hashable, tuple[float, V]] = {}
        self._key_locks: dict[Hashable, threading.Lock] = {}
        self._guard = threading.Lock()

    def _fresh(self, key: Hashable) -> tuple[bool, V | None]:
        entry = self._values.get(key)
        if entry is not None and entry[0] > self._clock():
            return True, entry[1]
        return False, None

    def _store(self, key: Hashable, value: V) -> None:
        self._values[key] = (self._clock() + self.ttl, value)

    def _lock_for(self, key: Hashable) -> threading.Lock:
        with self._guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = self._key_locks[key] = threading.Lock()
            return lock

    def get_or_compute(self, key: Hashable, compute: Callable[[], V]) -> V:
        hit, value = self._fresh(key)
        if hit:
            return value  # type: ignore[return-value]

        if not self.single_flight:
            fresh_value = compute()
            self._store(key, fresh_value)
            return fresh_value

        with self._lock_for(key):
            # Someone else may have filled it while we waited for the lock.
            hit, value = self._fresh(key)
            if hit:
                return value  # type: ignore[return-value]
            fresh_value = compute()
            self._store(key, fresh_value)
            return fresh_value

    def clear(self) -> None:
        self._values.clear()
