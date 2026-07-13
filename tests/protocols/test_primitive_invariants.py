# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Invariant/property tests for the four core primitives: Pile, Progression,
Element, and Graph.

Regression class this guards against: these primitives sit on every hot path
(message history, tool piles, the DAG executor) and keep attracting
performance-motivated cleanups — swapping a linear scan for a dict lookup,
caching a computed view, batching a lock acquisition. Each of those changes is
"obviously behavior preserving" in isolation, but the actual contracts here
(insertion-order iteration, Pile/Progression independence, exact serialization
round-tripping, adjacency-mapping consistency under mutation) are enforced by
convention across many call sites, not by a single invariant check. A cleanup
that quietly breaks ordering, thread-safety, serialization, or adjacency
consistency ships green and is only discovered downstream, far from the
change that caused it.

Every assertion below pins CURRENT, empirically observed behavior (including
behavior that looks like a footgun, e.g. KeyError on iterate-while-mutate) —
it is not aspirational. If a future change deliberately alters one of these
contracts, the test (and this docstring) should be updated alongside it.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError as PydanticValidationError

from lionagi._class_registry import LION_CLASS_REGISTRY
from lionagi._errors import ItemNotFoundError, RelationError
from lionagi.protocols.types import Edge, Element, Graph, Node, Pile, Progression


class Item(Element):
    value: int = 0


class MyNode(Node):
    """Named Node subclass so it self-registers in LION_CLASS_REGISTRY,
    letting the polymorphic-dispatch test resolve by registry lookup instead
    of depending on the test module's own import path."""


# ---------------------------------------------------------------------------
# 1. Pile — ordering, O(1) access, thread-safety, async iteration
# ---------------------------------------------------------------------------


class TestPileOrderingInvariants:
    def test_iteration_order_matches_progression(self):
        items = [Item(value=i) for i in range(20)]
        pile = Pile(items)
        assert [x.id for x in pile] == list(pile.progression)

    @given(
        st.lists(
            st.integers(min_value=0, max_value=999),
            min_size=0,
            max_size=30,
            unique=True,
        )
    )
    @settings(max_examples=50)
    def test_insertion_order_preserved_for_arbitrary_sequences(self, values):
        items = [Item(value=v) for v in values]
        pile = Pile(items)
        assert list(pile.progression) == [it.id for it in items]
        assert [x.id for x in pile] == [it.id for it in items]

    def test_getitem_by_uuid_returns_exact_object(self):
        items = [Item(value=i) for i in range(10)]
        pile = Pile(items)
        for it in items:
            got = pile[it.id]
            assert got is it  # exact identity, not a copy or a re-validated clone

    @given(data=st.data())
    @settings(max_examples=50)
    def test_arbitrary_include_then_exclude_subset_preserves_relative_order(self, data):
        n = data.draw(st.integers(min_value=1, max_value=25))
        items = [Item(value=i) for i in range(n)]
        pile = Pile()
        for it in items:
            pile.include(it)

        exclude_subset = data.draw(
            st.lists(st.sampled_from(items), unique_by=lambda x: x.id, max_size=n)
        )
        pile.exclude(exclude_subset)

        excluded_ids = {it.id for it in exclude_subset}
        remaining_ids = [it.id for it in items if it.id not in excluded_ids]

        assert list(pile.progression) == remaining_ids
        assert set(pile.collections.keys()) == set(remaining_ids)
        assert len(pile) == len(remaining_ids)


class TestPileThreadSafety:
    def test_concurrent_include_exclude_preserves_consistency(self):
        """20 threads including new items and 10 threads excluding pre-seeded
        items, run concurrently. @synchronized (self._lock, an RLock) must
        serialize include/exclude so collections and progression never drift
        apart, regardless of interleaving."""
        pile = Pile()
        items = [Item(value=i) for i in range(200)]
        to_pre_include = items[:100]
        to_concurrently_include = items[100:]
        pile.include(to_pre_include)

        errors: list[Exception] = []

        def include_task(it):
            try:
                pile.include(it)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def exclude_task(it):
            try:
                pile.exclude(it)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(include_task, it) for it in to_concurrently_include]
            futures += [pool.submit(exclude_task, it) for it in to_pre_include]
            for f in as_completed(futures):
                f.result()

        assert errors == []
        assert len(pile) == 100
        assert set(pile.progression) == {it.id for it in to_concurrently_include}
        # collections and progression never desync: same membership, no dupes
        assert len(pile.collections) == len(pile.progression) == len(set(pile.progression))

    def test_concurrent_pop_never_double_removes_or_loses_items(self):
        """Many threads racing pop() on overlapping keys: exactly the items
        that exist get removed exactly once; no KeyError leaks past pop's own
        ItemNotFoundError handling for a key already taken by another thread."""
        items = [Item(value=i) for i in range(150)]
        pile = Pile(items)
        removed: list = []
        lock = threading.Lock()

        def pop_task(it):
            try:
                got = pile.pop(it.id)
                with lock:
                    removed.append(got)
            except ItemNotFoundError:
                pass  # another thread already popped this id — acceptable

        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(pop_task, it) for it in items]
            for f in as_completed(futures):
                f.result()

        assert len(pile) == 0
        assert len(removed) == len(items)
        assert {r.id for r in removed} == {it.id for it in items}


class TestPileAsyncIteration:
    @pytest.mark.asyncio
    async def test_async_for_matches_progression_order(self):
        items = [Item(value=i) for i in range(15)]
        pile = Pile(items)
        collected = [x.id async for x in pile]
        assert collected == list(pile.progression)

    @pytest.mark.asyncio
    async def test_async_context_manager_serializes_concurrent_access(self):
        """`async with pile:` acquires pile.async_lock; two concurrent tasks
        entering it must not interleave — one fully enters and exits before
        the other enters."""
        pile = Pile([Item(value=1)])
        events: list[str] = []

        async def worker(name: str, delay: float):
            async with pile:
                events.append(f"{name}-enter")
                await asyncio.sleep(delay)
                events.append(f"{name}-exit")

        await asyncio.gather(worker("a", 0.05), worker("b", 0.0))

        assert events in (
            ["a-enter", "a-exit", "b-enter", "b-exit"],
            ["b-enter", "b-exit", "a-enter", "a-exit"],
        )

    @pytest.mark.asyncio
    async def test_async_pile_iterator_class_matches_sync_order(self):
        items = [Item(value=i) for i in range(8)]
        pile = Pile(items)
        it = Pile.AsyncPileIterator(pile)
        collected = [x.id async for x in it]
        assert collected == list(pile.progression)


class TestPileIterationMutationContract:
    """Pin the CURRENT (footgun) contract: Pile.__iter__ snapshots the id
    *order* up front (`current_order = list(self.progression)`) but resolves
    each id against `self.collections` lazily, on every step. Removing a
    not-yet-visited item mid-iteration does not raise at removal time — the
    KeyError only surfaces later, when the iterator reaches that id."""

    def test_removing_not_yet_visited_item_raises_keyerror_on_next_step(self):
        items = [Item(value=i) for i in range(5)]
        pile = Pile(items)

        it = iter(pile)
        first = next(it)
        assert first.id == items[0].id

        pile.pop(items[2].id)  # not yet visited — removal itself doesn't raise

        collected = [next(it)]  # items[1], not yet affected
        with pytest.raises(KeyError):
            collected.append(next(it))  # reaches items[2] -> KeyError

    def test_removing_already_visited_item_does_not_affect_iteration(self):
        items = [Item(value=i) for i in range(5)]
        pile = Pile(items)

        it = iter(pile)
        seen = [next(it).id, next(it).id]  # items[0], items[1]
        pile.pop(items[0].id)  # already-visited item removed — no effect on `it`

        remaining = [x.id for x in it]
        assert remaining == [i.id for i in items[2:]]
        assert seen == [items[0].id, items[1].id]


# ---------------------------------------------------------------------------
# 2. Progression / Pile independence
# ---------------------------------------------------------------------------


class TestProgressionPileIndependence:
    def test_multiple_progressions_over_one_pile_reorder_independently(self):
        items = [Item(value=i) for i in range(6)]
        pile = Pile(items)
        prog_a = Progression(order=[it.id for it in items])
        prog_b = Progression(order=[it.id for it in items])

        prog_a.reverse()

        assert list(prog_a) == list(reversed([it.id for it in items]))
        assert list(prog_b) == [it.id for it in items]  # untouched sibling
        assert list(pile.progression) == [it.id for it in items]  # untouched pile

    def test_removing_from_pile_does_not_mutate_a_detached_progression(self):
        """A Progression built by copying ids out of a Pile is a fully
        separate value object: it is never rewired by later Pile mutation."""
        items = [Item(value=i) for i in range(5)]
        pile = Pile(items)
        detached_prog = Progression(order=[it.id for it in items])

        removed_id = items[2].id
        pile.pop(removed_id)

        assert removed_id not in pile.progression
        assert removed_id in detached_prog
        assert len(detached_prog) == 5
        assert list(detached_prog) == [it.id for it in items]

    def test_dereferencing_a_pile_removed_id_via_a_detached_progression_raises(self):
        """The detached Progression still lists the id (pinned above), but
        using that id to look the object up in the Pile it came from raises
        ItemNotFoundError — the Progression's membership and the Pile's
        actual storage are two independent sources of truth."""
        items = [Item(value=i) for i in range(5)]
        pile = Pile(items)
        detached_prog = Progression(order=[it.id for it in items])
        removed_id = items[2].id
        pile.pop(removed_id)

        assert removed_id in detached_prog
        with pytest.raises(ItemNotFoundError):
            pile[removed_id]

    @given(st.lists(st.integers(min_value=0, max_value=99), min_size=2, max_size=20, unique=True))
    @settings(max_examples=30)
    def test_progression_move_and_swap_never_change_membership(self, values):
        items = [Item(value=v) for v in values]
        prog = Progression(order=[it.id for it in items])
        original_members = set(prog)

        prog.swap(0, len(prog) - 1)
        assert set(prog) == original_members
        assert len(prog) == len(items)

        if len(prog) >= 2:
            prog.move(0, len(prog) - 1)
            assert set(prog) == original_members
            assert len(prog) == len(items)


# ---------------------------------------------------------------------------
# 3. Element.to_dict / from_dict roundtrips
# ---------------------------------------------------------------------------


class TestElementToDictRoundtrip:
    @pytest.mark.parametrize("mode", ["python", "json", "db"])
    def test_roundtrip_preserves_identity_fields(self, mode):
        el = Item(value=42, metadata={"tag": "x"})
        dumped = el.to_dict(mode=mode)
        restored = Item.from_dict(dumped)
        assert restored.id == el.id
        assert restored.created_at == pytest.approx(el.created_at)
        assert restored.value == el.value
        assert restored.metadata.get("tag") == "x"

    def test_json_mode_is_json_dumps_able(self):
        el = Item(value=7)
        dumped = el.to_dict(mode="json")
        s = json.dumps(dumped)  # must survive the stdlib encoder, no custom default needed
        assert json.loads(s)["value"] == 7

    def test_db_mode_renames_metadata_key(self):
        el = Item(value=1, metadata={"a": 1})
        dumped = el.to_dict(mode="db")
        assert "metadata" not in dumped
        assert "node_metadata" in dumped
        assert dumped["node_metadata"]["a"] == 1

        restored = Item.from_dict(dumped)
        assert restored.metadata["a"] == 1
        assert restored.id == el.id

    def test_unsupported_mode_raises_value_error(self):
        el = Item()
        with pytest.raises(ValueError):
            el.to_dict(mode="xml")

    def test_unknown_field_rejected_extra_forbid(self):
        """Element.model_config sets extra='forbid' — a payload carrying a
        field the model doesn't declare must fail validation, not silently
        drop the field."""
        el = Item(value=1)
        dumped = el.to_dict(mode="python")
        dumped["totally_unexpected_field"] = "nope"
        with pytest.raises(PydanticValidationError):
            Item.from_dict(dumped)

    def test_element_from_dict_dispatches_to_the_concrete_subclass(self):
        """Element.from_dict (called on the BASE class) resolves the
        `lion_class` recorded in metadata via LION_CLASS_REGISTRY and
        delegates to the concrete subclass's own from_dict/model_validate —
        it does not just construct a bare Element."""
        node = MyNode(content={"a": 1})
        assert MyNode.class_name(full=True) in LION_CLASS_REGISTRY

        dumped = node.to_dict(mode="python")
        restored = Element.from_dict(dumped)

        assert type(restored) is MyNode
        assert restored.id == node.id
        assert restored.content == {"a": 1}

    def test_json_and_db_modes_reject_ints_outside_int64_range(self):
        """mode='python' serializes metadata via plain model_dump and
        tolerates arbitrary-precision Python ints. mode='json' and mode='db'
        both route through orjson.dumps, which only accepts 64-bit integers —
        a value one below int64-min raises TypeError in those two modes but
        not in 'python'. This asymmetry is a real, currently-shipping
        limitation of Element serialization worth pinning explicitly."""
        below_int64_min = -(2**63) - 1
        el = Item(value=1, metadata={"a": below_int64_min})

        dumped = el.to_dict(mode="python")
        assert dumped["metadata"]["a"] == below_int64_min

        with pytest.raises(TypeError):
            el.to_dict(mode="json")
        with pytest.raises(TypeError):
            el.to_dict(mode="db")

    @given(
        value=st.integers(min_value=-10_000, max_value=10_000),
        meta=st.dictionaries(
            st.text(
                min_size=1,
                max_size=8,
                alphabet=st.characters(whitelist_categories=("Ll", "Lu")),
            ),
            # bounded to the int64 range: mode="json"/"db" route through
            # orjson.dumps, which rejects wider ints (see
            # test_json_and_db_modes_reject_ints_outside_int64_range below).
            # An unbounded strategy here would fail on that orthogonal
            # limitation instead of exercising the roundtrip contract.
            st.integers(min_value=-(2**63), max_value=2**63 - 1),
            max_size=5,
        ),
    )
    @settings(max_examples=30)
    def test_roundtrip_property_arbitrary_values_and_metadata(self, value, meta):
        el = Item(value=value, metadata=dict(meta))
        for mode in ("python", "json", "db"):
            dumped = el.to_dict(mode=mode)
            restored = Item.from_dict(dumped)
            assert restored.id == el.id
            assert restored.value == value
            for k, v in meta.items():
                assert restored.metadata.get(k) == v


# ---------------------------------------------------------------------------
# 4. Graph adjacency consistency
# ---------------------------------------------------------------------------


class TestGraphAdjacencyConsistency:
    @staticmethod
    def _build_chain(n: int = 4):
        """n nodes in a straight chain: nodes[0] -> nodes[1] -> ... -> nodes[n-1]."""
        g = Graph()
        nodes = [Node() for _ in range(n)]
        for node in nodes:
            g.add_node(node)
        edges = [Edge(head=nodes[i], tail=nodes[i + 1]) for i in range(n - 1)]
        for e in edges:
            g.add_edge(e)
        return g, nodes, edges

    def test_add_node_populates_mapping_and_pile(self):
        g = Graph()
        node = Node()
        g.add_node(node)
        assert node.id in g.internal_nodes
        assert g.node_edge_mapping[node.id] == {"in": {}, "out": {}}

    def test_add_edge_updates_both_directions_of_mapping(self):
        g, nodes, edges = self._build_chain(3)
        e = edges[0]
        assert g.node_edge_mapping[e.head]["out"][e.id] == e.tail
        assert g.node_edge_mapping[e.tail]["in"][e.id] == e.head
        assert e.id in g.internal_edges

    def test_add_edge_rejects_dangling_endpoints(self):
        g = Graph()
        floating = Node()
        attached = Node()
        g.add_node(attached)
        with pytest.raises(RelationError):
            g.add_edge(Edge(head=floating, tail=attached))

    def test_remove_node_cascades_all_incident_edges(self):
        g, nodes, edges = self._build_chain(4)
        middle = nodes[1]
        g.remove_node(middle)

        for e in edges:
            if e.head == middle.id or e.tail == middle.id:
                assert e.id not in g.internal_edges
            else:
                assert e.id in g.internal_edges

        assert middle.id not in g.node_edge_mapping
        for e in edges:
            if e.head == middle.id:
                assert e.id not in g.node_edge_mapping[e.tail]["in"]
            if e.tail == middle.id:
                assert e.id not in g.node_edge_mapping[e.head]["out"]

    def test_remove_edge_updates_both_endpoints_without_touching_nodes(self):
        g, nodes, edges = self._build_chain(3)
        e = edges[0]
        g.remove_edge(e)
        assert e.id not in g.internal_edges
        assert e.id not in g.node_edge_mapping[e.head]["out"]
        assert e.id not in g.node_edge_mapping[e.tail]["in"]
        assert nodes[0].id in g.internal_nodes
        assert nodes[1].id in g.internal_nodes

    def test_get_predecessors_and_successors_match_mapping(self):
        g, nodes, edges = self._build_chain(4)
        for i in range(1, len(nodes) - 1):
            preds = {n.id for n in g.get_predecessors(nodes[i])}
            succs = {n.id for n in g.get_successors(nodes[i])}
            assert preds == {nodes[i - 1].id}
            assert succs == {nodes[i + 1].id}

    def test_predecessor_successor_are_fresh_snapshots_not_live_views(self):
        """get_successors/get_predecessors build a brand-new Pile on every
        call from the current node_edge_mapping. A Pile handed back before a
        mutation keeps its stale contents (it is a value, not a live view);
        calling the accessor again afterwards reflects the current state."""
        g, nodes, edges = self._build_chain(3)
        succ_before = g.get_successors(nodes[0])
        assert len(succ_before) == 1
        assert succ_before[0].id == nodes[1].id

        g.remove_node(nodes[1])

        # already-returned Pile is unaffected by the later mutation
        assert len(succ_before) == 1
        assert succ_before[0].id == nodes[1].id

        # a fresh call reflects the current, post-mutation graph
        succ_after = g.get_successors(nodes[0])
        assert len(succ_after) == 0

    def test_removing_node_mid_iteration_over_internal_nodes_raises_keyerror(self):
        """internal_nodes IS a Pile, so it inherits the exact iterate-while-
        mutate contract pinned in TestPileIterationMutationContract: removing
        a not-yet-visited node mid-loop raises KeyError once the iterator
        reaches it, rather than at the point of removal."""
        g, nodes, edges = self._build_chain(4)
        with pytest.raises(KeyError):
            for i, _node in enumerate(g.internal_nodes):
                if i == 0:
                    g.remove_node(nodes[2])

    def test_topological_sort_consistent_with_chain_order(self):
        g, nodes, edges = self._build_chain(5)
        order = g.topological_sort()
        assert [n.id for n in order] == [n.id for n in nodes]

    def test_is_acyclic_true_for_chain_false_after_adding_cycle(self):
        g, nodes, _edges = self._build_chain(3)
        assert g.is_acyclic() is True
        g.add_edge(Edge(head=nodes[-1], tail=nodes[0]))
        assert g.is_acyclic() is False

    def test_contains_checks_both_node_and_edge_piles(self):
        g, nodes, edges = self._build_chain(2)
        assert nodes[0] in g
        assert edges[0] in g
        assert Node() not in g


# File: tests/protocols/test_primitive_invariants.py
