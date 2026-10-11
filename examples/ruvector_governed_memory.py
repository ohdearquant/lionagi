"""Native RVF acceptance smoke: restart, expiry, evaluation and rollback.

Run: uv run --extra ruvector python examples/ruvector_governed_memory.py
This deterministic fixture is an API smoke test, not a performance benchmark or
proof of learning on unseen queries. Feedback persists; policy approval is runtime only.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from lionagi import Node
from lionagi.integrations.ruvector.evaluation import (
    EvaluationCase,
    EvaluationGate,
    GovernedFeedbackRanker,
)
from lionagi.integrations.ruvector.learning import FeedbackRanker
from lionagi.integrations.ruvector.lifecycle import LifecycleRuVectorStore


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def main() -> None:
    epoch = [100.0]
    # TemporaryDirectory is private to this user. Production must use a durable,
    # private application directory and a trusted epoch clock.
    with TemporaryDirectory(prefix="lionagi-governed-") as private:
        directory = Path(private) / "rvf"
        with LifecycleRuVectorStore(
            directory, 2, embedding_id="example-embedding-v1", clock=lambda: epoch[0]
        ) as store:
            near, preferred = await store.aadd_nodes(
                [
                    Node(content="Nearest draft solution", embedding=[1.0, 0.0]),
                    Node(content="Approved solution", embedding=[0.995, 0.1]),
                ]
            )
            temporary = (
                await store.aadd_nodes(
                    [Node(content="Temporary note", embedding=[0.0, 1.0])], ttl_seconds=5
                )
            )[0]
            candidate = FeedbackRanker(store)
            await asyncio.to_thread(
                candidate.feedback, "solution", preferred, 1.0, event_id="review-1"
            )
            cases = [EvaluationCase("solution", (1.0, i / 10000), (preferred,)) for i in range(20)]
            governed = GovernedFeedbackRanker(store)
            require(
                (await governed.asearch([1.0, 0.0], query="solution", k=1))[0].node_id == near,
                "Unapproved policy must use native retrieval",
            )
            # Relax latency only for this tiny API fixture; production gate defaults
            # to 1.5 and requires application supplied heldout cases.
            smoke_gate = EvaluationGate(max_p95_ratio=100)
            report = await governed.apromote(candidate, cases, smoke_gate, k=1)
            require(report.accepted, f"Fixture promotion failed: {report.reasons}")
            disabled_candidate = FeedbackRanker(store, max_boost=0)
            rejected = await governed.apromote(disabled_candidate, cases, smoke_gate, k=1)
            require(not rejected.accepted, "Unimproved candidate must fail")
            require(
                (await governed.asearch([1.0, 0.0], query="solution", k=1))[0].node_id == preferred,
                "Rejected candidate must preserve prior approved configuration",
            )
            require(governed.rollback(), "Rollback must restore baseline")
            require(
                (await governed.asearch([1.0, 0.0], query="solution", k=1))[0].node_id == near,
                "Rollback must restore native retrieval",
            )
            epoch[0] = 106.0
            require(
                await asyncio.to_thread(store.get, temporary) is None,
                "Expired record must be hidden",
            )
            require(await store.apurge_expired() == 1, "Purge must remove expired record")
            await store.acompact()
        with LifecycleRuVectorStore(
            directory, 2, embedding_id="example-embedding-v1", clock=lambda: epoch[0]
        ) as reopened:
            require(
                await asyncio.to_thread(reopened.get, preferred) is not None,
                "Memory must survive restart",
            )
            require(
                await asyncio.to_thread(reopened.get, temporary) is None,
                "Purged memory must stay deleted",
            )
            resumed = FeedbackRanker(reopened)
            require(
                (await resumed.asearch([1.0, 0.0], query="solution", k=1))[0].node_id == preferred,
                "Feedback must survive restart",
            )
            runtime = GovernedFeedbackRanker(reopened)
            require(
                (await runtime.asearch([1.0, 0.0], query="solution", k=1))[0].node_id == near,
                "New runtime must require fresh approval",
            )
        print(
            json.dumps(
                {
                    "acceptance": "passed",
                    "native_sdk": "ruvector",
                    "report": asdict(report),
                    "rejected_candidate_reasons": rejected.reasons,
                    "policy_approval": "runtime only",
                    "restart": "memory and feedback retained",
                    "lifecycle": "expired record purged and compacted",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
