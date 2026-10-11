# Copyright (c) 2026, Reuven Cohen
# SPDX-License-Identifier: Apache-2.0

"""Explicit, bounded relevance feedback; no model training or implicit feedback."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import numpy as np

from .store import SearchHit, _integer, _text

if TYPE_CHECKING:
    from .persistent import PersistentRuVectorStore

__all__ = ("FeedbackRanker",)


class FeedbackRanker:
    """Rerank eligible candidates using trusted, query-specific signed rewards.

    Applications must authorize feedback writes. Retrieved content and model output
    must never be treated as feedback automatically. The maximum score adjustment
    is bounded by ``max_boost``; returned distances remain original cosine distances.
    Disabling feedback restores the native search path. This is relevance feedback,
    not embedding training, generalized learning, or an authorization mechanism.
    """

    def __init__(
        self,
        store: PersistentRuVectorStore,
        *,
        max_boost: float = 0.1,
        prior: float = 4.0,
        candidate_multiplier: int = 4,
    ) -> None:
        from .persistent import PersistentRuVectorStore

        if not isinstance(store, PersistentRuVectorStore):
            raise TypeError("store must be a PersistentRuVectorStore")
        for value, name, low, high in (
            (max_boost, "max_boost", 0, 0.25),
            (prior, "prior", 1, 1000),
        ):
            if (
                type(value) not in (int, float)
                or not low <= value <= high
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be finite and between {low} and {high}")
        self._store = store
        self.max_boost = float(max_boost)
        self.prior = float(prior)
        self.candidate_multiplier = _integer(candidate_multiplier, "candidate_multiplier", 16)

    @property
    def store(self) -> PersistentRuVectorStore:
        """The store bound at construction; authorization scopes cannot be swapped."""
        return self._store

    def _context(self, query: str) -> str:
        query = _text(query, self.store.max_content_bytes, "query")
        # Length-delimited namespace prevents ambiguous concatenation. Raw queries
        # are not persisted, but a digest is not anonymization of predictable text.
        model = self.store.embedding_id.encode("utf-8")
        return hashlib.sha256(
            b"lionagi.ruvector.feedback.v1\0"
            + len(model).to_bytes(8, "big")
            + model
            + query.encode("utf-8")
        ).hexdigest()

    def feedback(self, query: str, node_id: str, reward: float, *, event_id: str) -> bool:
        """Record a host-authorized reward in [-1, 1], once per event ID."""
        return self.store.record_feedback(self._context(query), node_id, reward, event_id)

    def reset(self, query: str) -> int:
        """Delete this exact query's feedback and return the removed event count."""
        return self.store.reset_feedback(self._context(query))

    def search(
        self,
        vector: Sequence[float] | np.ndarray,
        *,
        query: str,
        k: int = 5,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        use_feedback: bool = True,
    ) -> list[SearchHit]:
        """Retrieve eligible candidates, then apply bounded feedback adjustments."""
        context = self._context(query)
        _integer(k, "k", self.store.max_k)
        if type(use_feedback) is not bool:
            raise ValueError("use_feedback must be boolean")
        with self.store._lock:
            if not use_feedback or self.max_boost == 0 or not self.store.has_feedback(context):
                return self.store.search(vector, k=k, predicate=predicate)
            size = min(self.store.max_k, k * self.candidate_multiplier)
            hits = self.store.search(vector, k=size, predicate=predicate)
            rewards = self.store.feedback(context, [hit.node_id for hit in hits])

            def score(hit: SearchHit) -> float:
                total, count = rewards.get(hit.node_id, (0.0, 0))
                return hit.distance - self.max_boost * total / (count + self.prior)

            # Python's stable sort retains original candidate order on score ties.
            return sorted(hits, key=score)[:k]

    async def asearch(
        self,
        vector: Sequence[float] | np.ndarray,
        *,
        query: str,
        k: int = 5,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        use_feedback: bool = True,
    ) -> list[SearchHit]:
        """Run search off the event loop; cancellation does not stop a running worker."""
        return await asyncio.to_thread(
            self.search, vector, query=query, k=k, predicate=predicate, use_feedback=use_feedback
        )
