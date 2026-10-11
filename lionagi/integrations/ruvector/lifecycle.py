# Copyright (c) 2026, Reuven Cohen
# SPDX-License-Identifier: Apache-2.0
"""Expiry and physical compaction for explicitly scoped RVF agent memory."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from lionagi.protocols.graph.node import Node

from .journaled import JournaledRuVectorStore
from .store import SearchHit

__all__ = ("LifecycleRuVectorStore",)
_KEY = "_ruvector_lifecycle"


def _finite(value: float, name: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


class LifecycleRuVectorStore(JournaledRuVectorStore):
    """Hide expired records on reads; purge explicitly to reclaim capacity.

    The trusted clock returns epoch seconds. Each read captures one time. Expiry
    metadata is reserved, persists across restart, and is hidden in public hits.
    Backward clock movement fails closed within this process. The application
    must supply a trusted epoch clock across process restarts.
    Physical capacity includes expired records until purge. Compaction cannot
    erase copies, filesystem snapshots, backups, or storage device remnants.
    """

    def __init__(
        self,
        directory: str | Path,
        dimensions: int,
        *,
        embedding_id: str,
        clock: Callable[[], float] = time.time,
        **store_options: Any,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._clock = clock
        self._last_clock: float | None = None
        self._evaluation_time: float | None = None
        self._now()
        super().__init__(directory, dimensions, embedding_id=embedding_id, **store_options)
        try:
            for _, metadata, _ in self._records.values():
                self._expiry(json.loads(metadata))
            self._refresh_expiry()
        except BaseException:
            self.close()
            raise

    def _refresh_expiry(self) -> None:
        expiries = [self._expiry(json.loads(record[1])) for record in self._records.values()]
        self._next_expiry = min((e for e in expiries if e is not None), default=math.inf)

    def _now(self) -> float:
        if getattr(self, "_evaluation_active", False) and self._evaluation_time is not None:
            return _finite(self._evaluation_time, "evaluation time")
        now = _finite(self._clock(), "clock")
        if self._last_clock is not None and now < self._last_clock:
            raise ValueError("clock moved backwards; recreate with a trusted epoch clock")
        self._last_clock = now
        return now

    @staticmethod
    def _expiry(metadata: dict[str, Any]) -> float | None:
        envelope = metadata.get(_KEY)
        if _KEY not in metadata:
            return None
        if (
            type(envelope) is not dict
            or set(envelope) != {"version", "expires_at"}
            or type(envelope["version"]) is not int
            or envelope["version"] != 1
        ):
            raise ValueError("invalid lifecycle metadata")
        return _finite(envelope["expires_at"], "expires_at")

    @staticmethod
    def _public(hit: SearchHit) -> SearchHit:
        metadata = dict(hit.metadata)
        metadata.pop(_KEY, None)
        return SearchHit(hit.node_id, hit.content, metadata, hit.distance)

    def _eligible(
        self, now: float, predicate: Callable[[dict[str, Any]], bool] | None
    ) -> Callable[[dict[str, Any]], bool]:
        def eligible(metadata: dict[str, Any]) -> bool:
            expiry = self._expiry(metadata)
            if expiry is not None and expiry <= now:
                return False
            public = dict(metadata)
            public.pop(_KEY, None)
            return predicate is None or predicate(public)

        return eligible

    def add_nodes(self, nodes: list[Node], *, ttl_seconds: float | None = None) -> list[str]:
        if type(nodes) is not list or not 1 <= len(nodes) <= self.max_batch_size:
            raise ValueError("nodes must be a nonempty bounded list")
        expiry = None
        if ttl_seconds is not None:
            ttl = _finite(ttl_seconds, "ttl_seconds")
            if ttl <= 0:
                raise ValueError("ttl_seconds must be positive")
            with self._lock:
                expiry = _finite(self._now() + ttl, "expires_at")
        prepared = []
        for node in nodes:
            if not isinstance(node, Node):
                raise ValueError("all entries must be LionAGI Nodes")
            self._metadata(node.metadata)
            if _KEY in node.metadata:
                raise ValueError("lifecycle metadata is reserved")
            metadata = json.loads(self._metadata(node.metadata))
            if expiry is not None:
                metadata[_KEY] = {"version": 1, "expires_at": expiry}
            prepared.append(node.model_copy(update={"metadata": metadata}))
        with self._lock:
            ids = super().add_nodes(prepared)
            if expiry is not None:
                self._next_expiry = min(self._next_expiry, expiry)
            return ids

    def delete(self, ids: list[str]) -> int:
        with self._lock:
            removed = super().delete(ids)
            if removed:
                self._refresh_expiry()
            return removed

    def __len__(self) -> int:
        with self._lock:
            self._check()
            eligible = self._eligible(self._now(), None)
            return sum(eligible(json.loads(data[1])) for data in self._records.values())

    def get(self, node_id: str) -> SearchHit | None:
        with self._lock:
            now = self._now()
            hit = super().get(node_id)
            if hit is None or not self._eligible(now, None)(hit.metadata):
                return None
            return self._public(hit)

    def scan(
        self, k: int = 5, *, predicate: Callable[[dict[str, Any]], bool] | None = None
    ) -> list[SearchHit]:
        with self._lock:
            eligible = self._eligible(self._now(), predicate)
            return [self._public(hit) for hit in super().scan(k, predicate=eligible)]

    def search(
        self, vector: Sequence[float] | np.ndarray, k: int = 5, *, predicate=None
    ) -> list[SearchHit]:
        with self._lock:
            now = self._now()
            eligible = (
                None
                if predicate is None and self._next_expiry > now
                else self._eligible(now, predicate)
            )
            return [self._public(hit) for hit in super().search(vector, k, predicate=eligible)]

    def record_feedback(self, context: str, node_id: str, reward: float, event_id: str) -> bool:
        with self._lock:
            self._check()
            if self.get(node_id) is None:
                raise ValueError("feedback requires a live node")
            return super().record_feedback(context, node_id, reward, event_id)

    def purge_expired(self, now: float | None = None) -> int:
        with self._lock:
            self._check()
            self._assert_writable()
            when = self._now() if now is None else _finite(now, "now")
            ids = [
                key
                for key, (_, metadata, _) in self._records.items()
                if not self._eligible(when, None)(json.loads(metadata))
            ]
            if not ids:
                return 0
            removed = super()._delete_many(ids)
            self._refresh_expiry()
            return removed

    def compact(self, *, purge_expired: bool = True) -> None:
        if type(purge_expired) is not bool:
            raise ValueError("purge_expired must be boolean")
        with self._lock:
            self._assert_writable()
            if purge_expired:
                self.purge_expired()
            return super().compact()

    async def aadd_nodes(self, nodes: list[Node], *, ttl_seconds: float | None = None) -> list[str]:
        return await asyncio.to_thread(self.add_nodes, nodes, ttl_seconds=ttl_seconds)

    async def apurge_expired(self, now: float | None = None) -> int:
        return await asyncio.to_thread(self.purge_expired, now)

    async def acompact(self, *, purge_expired: bool = True) -> None:
        return await asyncio.to_thread(self.compact, purge_expired=purge_expired)
