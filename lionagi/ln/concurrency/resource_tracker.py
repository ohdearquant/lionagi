# Copyright (c) 2025-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Lightweight resource tracking (debug aid)."""

from __future__ import annotations

import threading
import time
import weakref
from dataclasses import dataclass

__all__ = (
    "track_resource",
    "untrack_resource",
    "LeakInfo",
    "LeakTracker",
)


@dataclass(frozen=True, slots=True)
class LeakInfo:
    name: str
    kind: str | None
    created_at: float


class LeakTracker:
    def __init__(self) -> None:
        self._live: dict[int, LeakInfo] = {}
        self._lock = threading.Lock()

    def track(self, obj: object, *, name: str | None, kind: str | None) -> None:
        info = LeakInfo(name=name or f"obj-{id(obj)}", kind=kind, created_at=time.time())
        key = id(obj)

        def _finalizer(_key: int = key) -> None:
            with self._lock:
                self._live.pop(_key, None)

        with self._lock:
            self._live[key] = info
        weakref.finalize(obj, _finalizer)

    def untrack(self, obj: object) -> None:
        with self._lock:
            self._live.pop(id(obj), None)

    def live(self) -> list[LeakInfo]:
        with self._lock:
            return list(self._live.values())

    def clear(self) -> None:
        with self._lock:
            self._live.clear()


_TRACKER = LeakTracker()


def track_resource(obj: object, name: str | None = None, kind: str | None = None) -> None:
    _TRACKER.track(obj, name=name, kind=kind)


def untrack_resource(obj: object) -> None:
    _TRACKER.untrack(obj)
