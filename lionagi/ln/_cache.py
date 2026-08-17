# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

__all__ = ("BoundedLRUCache",)

K = TypeVar("K")
V = TypeVar("V")


class BoundedLRUCache(Generic[K, V]):
    """Thread-safe LRU cache with env-configurable max size."""

    __slots__ = ("_cache", "_lock", "_max_size")

    def __init__(self, max_size_env: str, default_max: int) -> None:
        self._max_size = int(os.environ.get(max_size_env, str(default_max)))
        self._cache: OrderedDict[K, V] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: K) -> V | None:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        return None

    def put(self, key: K, value: V) -> None:
        with self._lock:
            self._put_unlocked(key, value)

    def get_or_create(self, key: K, factory: Callable[[], V]) -> V:
        """Return one cached value, constructing it atomically on a miss."""
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            value = factory()
            self._put_unlocked(key, value)
            return value

    def _put_unlocked(self, key: K, value: V) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_size:
            try:
                self._cache.popitem(last=False)
            except KeyError:
                break

    def __contains__(self, key: K) -> bool:
        with self._lock:
            return key in self._cache
