# Copyright (c) 2026, Reuven Cohen
# SPDX-License-Identifier: Apache-2.0

"""Optional native retrieval. Install with ``uv add 'lionagi[ruvector]'``."""

from __future__ import annotations

try:
    from .evaluation import EvaluationCase, EvaluationGate, EvaluationReport, GovernedFeedbackRanker
    from .journaled import JournaledRuVectorStore
    from .learning import FeedbackRanker
    from .lifecycle import LifecycleRuVectorStore
    from .memory import RuVectorMemoryStore
    from .persistent import PersistentRuVectorStore
    from .store import RuVectorStore, SearchHit
except ModuleNotFoundError as exc:
    if exc.name not in {"ruvector", "numpy"}:
        raise
    raise ImportError(
        "RuVector integration requires the optional extra: uv add 'lionagi[ruvector]'"
    ) from exc

__all__ = (
    "EvaluationCase",
    "EvaluationGate",
    "EvaluationReport",
    "GovernedFeedbackRanker",
    "JournaledRuVectorStore",
    "LifecycleRuVectorStore",
    "FeedbackRanker",
    "PersistentRuVectorStore",
    "RuVectorMemoryStore",
    "RuVectorStore",
    "SearchHit",
)
