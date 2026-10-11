# Copyright (c) 2026, Reuven Cohen
# SPDX-License-Identifier: Apache-2.0
"""Host supplied relevance evaluations and reversible runtime policy promotion."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .learning import FeedbackRanker
from .store import SearchHit, _integer

__all__ = ("EvaluationCase", "EvaluationReport", "EvaluationGate", "GovernedFeedbackRanker")


@dataclass(frozen=True)
class EvaluationCase:
    """Trusted labels and vectors. The application owns the heldout partition.

    Predicates must be deterministic and side effect free. Labels must refer to
    existing eligible records. Exact query feedback does not imply generalization.
    """

    query: str
    vector: tuple[float, ...]
    relevant_ids: tuple[str, ...]
    predicate: Callable[[dict[str, Any]], bool] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query, str):
            raise ValueError("query must be text")
        if (
            not isinstance(self.vector, Sequence)
            or isinstance(self.vector, (str, bytes))
            or not 1 <= len(self.vector) <= 8192
        ):
            raise ValueError("vector must be a bounded sequence")
        if (
            not isinstance(self.relevant_ids, Sequence)
            or isinstance(self.relevant_ids, (str, bytes))
            or not 1 <= len(self.relevant_ids) <= 1000
        ):
            raise ValueError("relevant_ids must be a bounded sequence")
        vector = tuple(self.vector)
        ids = tuple(self.relevant_ids)
        if not vector or len(vector) > 65536:
            raise ValueError("vector must be nonempty and bounded")
        if any(
            type(x) not in (int, float) or not -1e308 <= x <= 1e308 or not math.isfinite(x)
            for x in vector
        ):
            raise ValueError("vector must contain finite numbers")
        if not 1 <= len(ids) <= 1000 or any(not isinstance(x, str) or not x for x in ids):
            raise ValueError("relevant_ids must contain bounded nonempty IDs")
        if len(set(ids)) != len(ids):
            raise ValueError("relevant_ids must be unique")
        if self.predicate is not None and not callable(self.predicate):
            raise ValueError("predicate must be callable")
        object.__setattr__(self, "vector", vector)
        object.__setattr__(self, "relevant_ids", ids)


@dataclass(frozen=True)
class EvaluationReport:
    """Immutable aggregate evidence; no raw query, labels or content are included."""

    cases: int
    k: int
    baseline_mrr: float
    candidate_mrr: float
    baseline_p95_ms: float
    candidate_p95_ms: float
    regressions: int
    accepted: bool
    reasons: tuple[str, ...]
    embedding_id: str
    configuration: tuple[tuple[str, float], ...]
    gate_configuration: tuple[tuple[str, float], ...]
    dataset_digest: str
    # Digest covers query, vectors and labels, not executable predicates or RVF content.


def _mrr(hits: list[SearchHit], relevant: tuple[str, ...]) -> float:
    labels = set(relevant)
    return next((1 / i for i, hit in enumerate(hits, 1) if hit.node_id in labels), 0.0)


def _policy(ranker: FeedbackRanker) -> FeedbackRanker:
    # Snapshot mutable host configuration. The evaluated instance cannot subsequently
    # be changed by modifying the caller's candidate.
    return FeedbackRanker(
        ranker.store,
        max_boost=ranker.max_boost,
        prior=ranker.prior,
        candidate_multiplier=ranker.candidate_multiplier,
    )


class EvaluationGate:
    """Compare native retrieval and explicit feedback on the same frozen store.

    Acceptance is evidence for this application supplied dataset only. There is
    no training, automatic feedback write, or proof of an unseen query partition.
    """

    def __init__(
        self,
        *,
        min_cases: int = 20,
        min_relative_improvement: float = 0.1,
        max_p95_ratio: float = 1.5,
        allow_case_regressions: bool = False,
    ) -> None:
        self.min_cases = _integer(min_cases, "min_cases", 1000)
        for value, name, low, high in (
            (min_relative_improvement, "min_relative_improvement", 0, 10),
            (max_p95_ratio, "max_p95_ratio", 1, 100),
        ):
            if (
                type(value) not in (int, float)
                or not low <= value <= high
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} outside supported finite bounds")
        if type(allow_case_regressions) is not bool:
            raise ValueError("allow_case_regressions must be boolean")
        self.min_relative_improvement = float(min_relative_improvement)
        self.max_p95_ratio = float(max_p95_ratio)
        self.allow_case_regressions = allow_case_regressions

    def evaluate(
        self,
        candidate: FeedbackRanker,
        cases: Sequence[EvaluationCase],
        *,
        k: int = 5,
    ) -> EvaluationReport:
        if type(candidate) is not FeedbackRanker:
            raise TypeError("candidate must be a plain FeedbackRanker")
        if not isinstance(cases, Sequence) or not 1 <= len(cases) <= 1000:
            raise ValueError("cases must contain between 1 and 1000 cases")
        cases = tuple(cases)
        if any(not isinstance(case, EvaluationCase) for case in cases):
            raise TypeError("cases must be EvaluationCase instances")
        ranker = _policy(candidate)
        _integer(k, "k", ranker.store.max_k)
        configuration = (
            ("max_boost", ranker.max_boost),
            ("prior", ranker.prior),
            ("candidate_multiplier", float(ranker.candidate_multiplier)),
        )
        scores: dict[bool, list[float]] = {False: [], True: []}
        durations: dict[bool, list[float]] = {False: [], True: []}
        digest = hashlib.sha256()
        from .lifecycle import LifecycleRuVectorStore

        with ranker.store._lock:
            ranker.store._check()
            # Store mutation guards reject reentrant writes from host predicates.
            previous_guard = getattr(ranker.store, "_evaluation_active", False)
            lifecycle_store = (
                ranker.store if isinstance(ranker.store, LifecycleRuVectorStore) else None
            )
            previous_time = (
                lifecycle_store._evaluation_time if lifecycle_store is not None else None
            )
            if lifecycle_store is not None:
                lifecycle_store._evaluation_time = lifecycle_store._now()
            ranker.store._evaluation_active = True
            try:
                for index, case in enumerate(cases):
                    ranker._context(case.query)
                    if len(case.vector) != ranker.store.dimensions:
                        raise ValueError("case vector dimensions mismatch")
                    for node_id in case.relevant_ids:
                        hit = ranker.store.get(node_id)
                        if hit is None or (
                            case.predicate is not None and not case.predicate(hit.metadata)
                        ):
                            raise ValueError("relevant ID is missing or ineligible")
                    digest.update(
                        json.dumps(
                            (case.query, case.vector, case.relevant_ids),
                            separators=(",", ":"),
                            ensure_ascii=True,
                        ).encode()
                    )
                    # Warm both paths; paired measurement order alternates by case.
                    for enabled in (False, True):
                        ranker.search(
                            case.vector,
                            query=case.query,
                            k=k,
                            predicate=case.predicate,
                            use_feedback=enabled,
                        )
                    for enabled in (False, True) if index % 2 == 0 else (True, False):
                        start = time.perf_counter_ns()
                        hits = ranker.search(
                            case.vector,
                            query=case.query,
                            k=k,
                            predicate=case.predicate,
                            use_feedback=enabled,
                        )
                        elapsed = time.perf_counter_ns() - start
                        if elapsed < 0:
                            raise RuntimeError("evaluation clock moved backwards")
                        durations[enabled].append(elapsed / 1_000_000)
                        scores[enabled].append(_mrr(hits, case.relevant_ids))
            finally:
                ranker.store._evaluation_active = previous_guard
                if lifecycle_store is not None:
                    lifecycle_store._evaluation_time = previous_time
        baseline = sum(scores[False]) / len(cases)
        candidate_score = sum(scores[True]) / len(cases)
        baseline_p95 = float(np.percentile(durations[False], 95))
        candidate_p95 = float(np.percentile(durations[True], 95))
        regressions = sum(a < b for a, b in zip(scores[True], scores[False], strict=True))
        reasons = []
        if len(cases) < self.min_cases:
            reasons.append("insufficient_cases")
        if candidate_score <= baseline or (
            baseline > 0 and candidate_score < baseline * (1 + self.min_relative_improvement)
        ):
            reasons.append("insufficient_quality_improvement")
        if regressions and not self.allow_case_regressions:
            reasons.append("case_quality_regression")
        if candidate_p95 > baseline_p95 * self.max_p95_ratio:
            reasons.append("latency_regression")
        return EvaluationReport(
            len(cases),
            k,
            baseline,
            candidate_score,
            baseline_p95,
            candidate_p95,
            regressions,
            not reasons,
            tuple(reasons),
            ranker.store.embedding_id,
            configuration,
            (
                ("min_cases", float(self.min_cases)),
                ("min_relative_improvement", self.min_relative_improvement),
                ("max_p95_ratio", self.max_p95_ratio),
                ("allow_case_regressions", float(self.allow_case_regressions)),
            ),
            digest.hexdigest(),
        )


class GovernedFeedbackRanker(FeedbackRanker):
    """Runtime policy gate, compatible with RuVectorMemoryStore.

    Starts with native retrieval. Failed promotion keeps the prior approved policy.
    Rollback restores the preceding policy, then native retrieval. Configuration
    approvals are process local; feedback remains durable in the bound RVF store.
    """

    def __init__(self, store: Any) -> None:
        super().__init__(store)
        self._approved: FeedbackRanker | None = None
        self._history: list[FeedbackRanker | None] = []

    def promote(
        self,
        candidate: FeedbackRanker,
        cases: Sequence[EvaluationCase],
        gate: EvaluationGate | None = None,
        *,
        k: int = 5,
    ) -> EvaluationReport:
        if type(candidate) is not FeedbackRanker or candidate.store is not self.store:
            raise ValueError("candidate must bind the same store")
        if gate is not None and type(gate) is not EvaluationGate:
            raise TypeError("gate must be an EvaluationGate")
        with self.store._lock:
            snapshot = _policy(candidate)
            report = (gate or EvaluationGate()).evaluate(snapshot, cases, k=k)
            if report.accepted:
                # Bound runtime history and avoid accumulating report contents.
                self._history = (self._history + [self._approved])[-16:]
                self._approved = snapshot
            return report

    def rollback(self) -> bool:
        with self.store._lock:
            had_policy = self._approved is not None
            self._approved = self._history.pop() if self._history else None
            return had_policy

    def search(
        self,
        vector: Sequence[float] | np.ndarray,
        *,
        query: str,
        k: int = 5,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        use_feedback: bool = True,
    ) -> list[SearchHit]:
        if type(use_feedback) is not bool:
            raise ValueError("use_feedback must be boolean")
        with self.store._lock:
            if self._approved is None:
                return super().search(
                    vector, query=query, k=k, predicate=predicate, use_feedback=False
                )
            return self._approved.search(
                vector,
                query=query,
                k=k,
                predicate=predicate,
                use_feedback=use_feedback,
            )

    async def apromote(
        self,
        candidate: FeedbackRanker,
        cases: Sequence[EvaluationCase],
        gate: EvaluationGate | None = None,
        *,
        k: int = 5,
    ) -> EvaluationReport:
        """Run evaluation off the event loop; cancellation cannot stop a running worker."""
        return await asyncio.to_thread(self.promote, candidate, cases, gate, k=k)
