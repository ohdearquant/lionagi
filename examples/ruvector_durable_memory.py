"""Run with a native SDK wheel providing RVF metadata and vector export APIs."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from lionagi import Node
from lionagi.integrations.ruvector import FeedbackRanker, PersistentRuVectorStore


def main() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "memory.rvf"
        options = {"embedding_id": "example-v1:3", "capacity": 10}
        with PersistentRuVectorStore(path, 3, **options) as store:
            nodes = [
                Node(content="general answer", embedding=[1.0, 0.0, 0.0]),
                Node(content="preferred answer", embedding=[0.98, 0.2, 0.0]),
            ]
            ids = store.add_nodes(nodes)
            ranker = FeedbackRanker(store)
            assert ranker.search([1, 0, 0], query="question", k=1)[0].node_id == ids[0]
            for index in range(8):
                ranker.feedback("question", ids[1], 1.0, event_id=f"rating-{index}")
        with PersistentRuVectorStore(path, 3, **options) as store:
            ranker = FeedbackRanker(store)
            assert ranker.search([1, 0, 0], query="question", k=1)[0].node_id == ids[1]
            assert ranker.reset("question") == 8
            assert ranker.search([1, 0, 0], query="question", k=1)[0].node_id == ids[0]
            print("RVF restart, feedback adaptation and reset verified")


if __name__ == "__main__":
    main()
