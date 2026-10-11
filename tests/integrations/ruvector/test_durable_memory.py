"""Durable LionAGI MemoryStore and explicit feedback integration contract."""

import asyncio

import pytest

ruvector = pytest.importorskip("ruvector")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        any(
            not hasattr(getattr(ruvector, "RvfStore", None), name)
            for name in ("set_metadata", "get_metadata", "export_vectors", "metadata_recovery")
        ),
        reason="installed RuVector SDK lacks native RVF checkpoint APIs",
    ),
]
from lionagi.integrations.ruvector import (
    FeedbackRanker,
    PersistentRuVectorStore,
    RuVectorMemoryStore,
)
from lionagi.protocols.memory import MemoryItem, MemoryQuery


async def embed(text):
    return [1.0, 0.2] if text == "preferred" else [1.0, 0.0]


def test_durable_memory_preserves_uuid_tags_timestamp_payload(tmp_path):
    async def run():
        path = tmp_path / "memory.rvf"
        item = MemoryItem(
            content="preferred",
            tags=["deployment", "private"],
            metadata={"owner": "one", "nested": [1, {"safe": True}]},
        )
        with PersistentRuVectorStore(path, 2, embedding_id="fixture-v1") as store:
            memory = RuVectorMemoryStore(store, embed)
            ident = await memory.store(item)
            assert ident == item.id
        with PersistentRuVectorStore(path, 2, embedding_id="fixture-v1") as store:
            memory = RuVectorMemoryStore(store, embed)
            result = await memory.retrieve(item.id)
            assert result.id == item.id
            assert result.created_at == item.created_at
            assert result.content == item.content
            assert result.tags == item.tags
            assert result.metadata == item.metadata
            assert [
                x.id
                for x in await memory.search(
                    MemoryQuery(text="query", tags=["deployment"], filters={"owner": "one"})
                )
            ] == [item.id]
            assert await memory.search(MemoryQuery(text="query", tags=["other"])) == []
            result.metadata["nested"].append("changed")
            assert (await memory.retrieve(item.id)).metadata == item.metadata

    asyncio.run(run())


def test_memory_feedback_opt_in_reopen_reset_and_filter_isolation(tmp_path):
    async def run():
        path = tmp_path / "feedback.rvf"
        closest = MemoryItem(content="closest", tags=["allowed"])
        preferred = MemoryItem(content="preferred", tags=["private"])
        with PersistentRuVectorStore(path, 2, embedding_id="fixture-v1") as store:
            default = RuVectorMemoryStore(store, embed)
            await default.store(closest)
            await default.store(preferred)
            ranker = FeedbackRanker(store)
            adapted = RuVectorMemoryStore(store, embed, ranker=ranker)
            for event in range(8):
                ranker.feedback("query", str(preferred.id), 1.0, event_id=f"event-{event}")
            assert (await default.search(MemoryQuery(text="query", limit=1)))[0].id == closest.id
            assert (await adapted.search(MemoryQuery(text="query", limit=1)))[0].id == preferred.id
            assert (await adapted.search(MemoryQuery(text="unseen", limit=1)))[0].id == closest.id
            assert (await adapted.search(MemoryQuery(text="query", tags=["allowed"], limit=1)))[
                0
            ].id == closest.id
        with PersistentRuVectorStore(path, 2, embedding_id="fixture-v1") as store:
            ranker = FeedbackRanker(store)
            adapted = RuVectorMemoryStore(store, embed, ranker=ranker)
            assert (await adapted.search(MemoryQuery(text="query", limit=1)))[0].id == preferred.id
            assert ranker.reset("query") == 8
            assert (await adapted.search(MemoryQuery(text="query", limit=1)))[0].id == closest.id

    asyncio.run(run())


def test_memory_rejects_ranker_bound_to_another_store(tmp_path):
    with (
        PersistentRuVectorStore(tmp_path / "first.rvf", 2, embedding_id="fixture-v1") as first,
        PersistentRuVectorStore(tmp_path / "second.rvf", 2, embedding_id="fixture-v1") as second,
    ):
        with pytest.raises(ValueError, match="store"):
            RuVectorMemoryStore(first, embed, ranker=FeedbackRanker(second))
