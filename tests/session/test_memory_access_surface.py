# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Branch/Session memory access surface (ADR-0090 slice 2):
the `memory=` constructor param, the lazy read-only `.memory` property, and
`Session.include_branches()` wiring a shared store into branches that don't
already have their own."""

from uuid import UUID

import pytest

from lionagi.protocols.memory import InMemoryStore, MemoryItem, MemoryQuery, MemoryStore
from lionagi.session.branch import Branch
from lionagi.session.session import Session


class _FakeMemoryStore:
    """Satisfies `MemoryStore` structurally with no inheritance, proving the
    access surface doesn't require subclassing `InMemoryStore`."""

    def __init__(self) -> None:
        self._data: dict[UUID, MemoryItem] = {}

    async def store(self, item: MemoryItem) -> UUID:
        self._data[item.id] = item
        return item.id

    async def retrieve(self, item_id: UUID) -> MemoryItem | None:
        return self._data.get(item_id)

    async def search(self, query: MemoryQuery) -> list[MemoryItem]:
        return list(self._data.values())[: query.limit]


class TestBranchMemorySurface:
    def test_standalone_branch_gets_lazy_default_with_zero_config(self):
        branch = Branch()
        assert branch._memory is None  # not created until first access
        store = branch.memory
        assert isinstance(store, InMemoryStore)
        assert branch._memory is store  # cached, same instance on repeat access
        assert branch.memory is store

    async def test_standalone_branch_memory_is_usable_immediately(self):
        branch = Branch()
        item_id = await branch.memory.store(MemoryItem(content="hello"))
        got = await branch.memory.search(MemoryQuery(text="hello"))
        assert any(r.id == item_id for r in got)

    def test_constructor_param_is_accepted_and_kept(self):
        own_store = InMemoryStore()
        branch = Branch(memory=own_store)
        assert branch._memory is own_store
        assert branch.memory is own_store

    def test_constructor_accepts_a_bare_protocol_implementor(self):
        fake = _FakeMemoryStore()
        branch = Branch(memory=fake)
        assert isinstance(branch.memory, MemoryStore)
        assert branch.memory is fake

    def test_memory_has_no_public_setter(self):
        branch = Branch()
        with pytest.raises(AttributeError):
            branch.memory = InMemoryStore()


class TestSessionMemorySurface:
    def test_standalone_session_gets_lazy_default(self):
        session = Session()
        assert session._memory is None
        store = session.memory
        assert isinstance(store, InMemoryStore)
        assert session.memory is store

    def test_constructor_param_is_accepted_and_kept(self):
        own_store = InMemoryStore()
        session = Session(memory=own_store)
        assert session.memory is own_store

    def test_memory_has_no_public_setter(self):
        session = Session()
        with pytest.raises(AttributeError):
            session.memory = InMemoryStore()

    def test_include_branches_shares_one_store_across_branches(self):
        session = Session()
        b1 = Branch(name="b1")
        b2 = Branch(name="b2")

        session.include_branches([b1, b2])

        assert b1.memory is session.memory
        assert b2.memory is session.memory

    async def test_writes_through_one_branch_are_visible_through_another(self):
        session = Session()
        b1 = Branch(name="b1")
        b2 = Branch(name="b2")
        session.include_branches([b1, b2])

        item_id = await b1.memory.store(MemoryItem(content="cross-branch"))
        retrieved = await b2.memory.retrieve(item_id)

        assert retrieved is not None
        assert retrieved.content == "cross-branch"

    def test_branch_explicitly_constructed_with_its_own_store_keeps_it(self):
        session = Session()
        own_store = InMemoryStore()
        branch = Branch(name="solo", memory=own_store)

        session.include_branches(branch)

        assert branch.memory is own_store
        assert branch.memory is not session.memory

    def test_branch_already_sharing_another_sessions_store_is_not_stolen(self):
        session_a = Session()
        session_b = Session()
        shared = Branch(name="shared")

        session_a.include_branches(shared)
        store_from_a = shared.memory

        session_b.include_branches(shared)

        assert shared.memory is store_from_a
        assert shared.memory is not session_b.memory

    def test_default_branch_created_lazily_by_session_gets_the_shared_store(self):
        session = Session()
        branch = Branch(name="lazy")
        session.include_branches(branch)

        assert branch._memory is not None
        assert branch.memory is session.memory
