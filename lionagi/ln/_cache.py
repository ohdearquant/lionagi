# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Generic, TypeVar

__all__ = ("BoundedLRUCache",)

K = TypeVar("K")
V = TypeVar("V")


class BoundedLRUCache(Generic[K, V]):
    """Thread-safe LRU cache with env-configurable max size.

    Under ``weak_values`` each value is held weakly, so an entry is never the
    reason a value is still alive, nor the reason anything the value reaches is.
    Sharing is unchanged for as long as anything else holds it, which is all a
    share cache is for; once nothing does, the entry reads as a miss and the next
    caller builds it again. A value that cannot be weakly referenced is not stored
    at all: holding it would mean holding it strongly, which is the one thing this
    mode exists to refuse.
    """

    __slots__ = ("_cache", "_lock", "_max_size", "_weak_values")

    def __init__(self, max_size_env: str, default_max: int, *, weak_values: bool = False) -> None:
        self._max_size = int(os.environ.get(max_size_env, str(default_max)))
        self._cache: OrderedDict[K, Any] = OrderedDict()
        self._lock = threading.RLock()
        self._weak_values = weak_values

    def _get_unlocked(self, key: K) -> tuple[bool, V | None]:
        """``(hit, value)``. A weak entry whose value is gone is not a hit."""
        if key not in self._cache:
            return False, None
        stored = self._cache[key]
        if self._weak_values:
            value = stored()
            if value is None:
                # The entry stands for nothing now. Dropping it here rather than
                # leaving it for eviction keeps the size bound honest and stops a
                # live key reporting a hit that resolves to nothing.
                del self._cache[key]
                return False, None
        else:
            value = stored
        self._cache.move_to_end(key)
        return True, value

    def get(self, key: K) -> V | None:
        with self._lock:
            return self._get_unlocked(key)[1]

    def put(self, key: K, value: V) -> None:
        with self._lock:
            self._put_unlocked(key, value)

    def get_or_create(self, key: K, factory: Callable[[], V]) -> V:
        """Return one cached value, constructing it atomically on a miss."""
        with self._lock:
            hit, value = self._get_unlocked(key)
            if hit:
                return value  # type: ignore[return-value]
            value = factory()
            self._put_unlocked(key, value)
            return value

    def _put_unlocked(self, key: K, value: V) -> None:
        if self._weak_values:
            try:
                stored: Any = weakref.ref(value)
            except TypeError:
                # Not storable on this cache's terms. Drop any entry already under
                # the key so nothing older is served in its place.
                self._cache.pop(key, None)
                return
        else:
            stored = value
        self._cache[key] = stored
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_size:
            try:
                self._cache.popitem(last=False)
            except KeyError:
                break

    def __contains__(self, key: K) -> bool:
        with self._lock:
            return self._get_unlocked(key)[0]
