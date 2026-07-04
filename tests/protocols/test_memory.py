# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Protocol-level contract fence for `MemoryStore`.

Runs identically against every backend that structurally satisfies
`MemoryStore` (the zero-dependency `InMemoryStore` and a bare-bones fake that
implements the Protocol with no inheritance from `InMemoryStore` at all), so a
future backend can drop in and run the same fence. Only asserts guarantees
that hold regardless of backend (store-then-retrieve fidelity); search
fidelity and read-after-write timing are backend-specific and are asserted
only in `InMemoryStore`'s own tests below, never here.
"""

from uuid import UUID, uuid4

import pytest

from lionagi.protocols.memory import InMemoryStore, MemoryItem, MemoryQuery, MemoryStore


class _FakeMemoryStore:
    """A minimal `MemoryStore` implementor with no relation to `InMemoryStore`.

    Proves the fence exercises the Protocol itself, not `InMemoryStore`'s
    concrete behavior.
    """

    def __init__(self) -> None:
        self._data: dict[UUID, MemoryItem] = {}

    async def store(self, item: MemoryItem) -> UUID:
        self._data[item.id] = item
        return item.id

    async def retrieve(self, item_id: UUID) -> MemoryItem | None:
        return self._data.get(item_id)

    async def search(self, query: MemoryQuery) -> list[MemoryItem]:
        return list(self._data.values())[: query.limit]


@pytest.fixture(params=[InMemoryStore, _FakeMemoryStore], ids=["InMemoryStore", "FakeMemoryStore"])
def store(request) -> MemoryStore:
    return request.param()


class TestMemoryStoreProtocolFence:
    def test_satisfies_protocol_structurally(self, store):
        assert isinstance(store, MemoryStore)

    async def test_store_returns_the_items_own_uuid(self, store):
        item = MemoryItem(content="hello")
        returned_id = await store.store(item)
        assert isinstance(returned_id, UUID)
        assert returned_id == item.id

    async def test_store_then_retrieve_fidelity(self, store):
        item = MemoryItem(content={"k": "v"}, tags=["a", "b"])
        item_id = await store.store(item)

        retrieved = await store.retrieve(item_id)

        assert retrieved is not None
        assert retrieved.id == item.id
        assert retrieved.content == item.content
        assert retrieved.tags == item.tags

    async def test_retrieve_unknown_id_returns_none(self, store):
        assert await store.retrieve(uuid4()) is None


class TestInMemoryStoreBackendSpecific:
    """`InMemoryStore`-only guarantees: exact substring/tag search and
    immediate read-after-write visibility. Not part of the cross-backend
    fence above -- a networked/async-indexed backend (e.g. one warming an ANN
    index) is not required to satisfy either of these.
    """

    async def test_search_respects_limit(self):
        store = InMemoryStore()
        for i in range(5):
            await store.store(MemoryItem(content=f"item-{i}"))

        results = await store.search(MemoryQuery(limit=2))

        assert isinstance(results, list)
        assert len(results) <= 2

    async def test_search_is_immediately_consistent_after_store(self):
        store = InMemoryStore()
        item = MemoryItem(content="just written")

        await store.store(item)
        results = await store.search(MemoryQuery(text="just written"))

        assert any(r.id == item.id for r in results)

    async def test_search_text_is_exact_substring_match(self):
        store = InMemoryStore()
        await store.store(MemoryItem(content="the quick brown fox"))
        await store.store(MemoryItem(content="a slow turtle"))

        results = await store.search(MemoryQuery(text="quick"))

        assert len(results) == 1
        assert "quick" in results[0].content

    async def test_search_by_tags(self):
        store = InMemoryStore()
        item_a = MemoryItem(content="a", tags=["work"])
        item_b = MemoryItem(content="b", tags=["personal"])
        await store.store(item_a)
        await store.store(item_b)

        results = await store.search(MemoryQuery(tags=["work"]))

        assert [r.id for r in results] == [item_a.id]

    async def test_search_by_metadata_filters(self):
        store = InMemoryStore()
        item = MemoryItem(content="a", metadata={"branch_id": "abc"})
        await store.store(item)
        await store.store(MemoryItem(content="b", metadata={"branch_id": "xyz"}))

        results = await store.search(MemoryQuery(filters={"branch_id": "abc"}))

        assert [r.id for r in results] == [item.id]

    async def test_search_with_no_criteria_returns_everything_up_to_limit(self):
        store = InMemoryStore()
        for i in range(3):
            await store.store(MemoryItem(content=f"item-{i}"))

        results = await store.search(MemoryQuery())

        assert len(results) == 3
