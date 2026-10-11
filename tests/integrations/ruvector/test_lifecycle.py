# SPDX-License-Identifier: Apache-2.0
"""Lifecycle expiry, filtering and durable reclamation contracts."""

import math

import pytest

pytest.importorskip("ruvector")
pytestmark = pytest.mark.integration

from lionagi import Node
from lionagi.integrations.ruvector.lifecycle import LifecycleRuVectorStore


def make(path, clock, **kwargs):
    return LifecycleRuVectorStore(path, 3, embedding_id="test:v1", clock=clock, **kwargs)


def node(tenant="a"):
    return Node(
        content="memory", metadata={"tenant": tenant, "nested": {"value": [1]}}, embedding=[1, 0, 0]
    )


def test_expiry_boundary_all_reads_and_feedback(tmp_path):
    current = [100.0]
    n = node()
    key = str(n.id)
    with make(tmp_path / "store", lambda: current[0]) as store:
        store.add_nodes([n], ttl_seconds=10)
        assert store.get(key).metadata == n.metadata
        assert "_ruvector_lifecycle" not in n.metadata
        assert len(store) == 1
        current[0] = 110.0
        assert store.get(key) is None
        assert store.scan() == []
        assert store.search([1, 0, 0]) == []
        assert len(store) == 0
        with pytest.raises(ValueError, match="live"):
            store.record_feedback("context", key, 1, "event")
        assert key in store._records


def test_filters_only_receive_public_metadata(tmp_path):
    current = [100.0]
    with make(tmp_path / "store", lambda: current[0]) as store:
        expiring, permanent = node("a"), node("b")
        store.add_nodes([expiring], ttl_seconds=5)
        store.add_nodes([permanent])
        seen = []

        def predicate(metadata):
            seen.append(metadata)
            return metadata["tenant"] == "b"

        assert [h.node_id for h in store.search([1, 0, 0], predicate=predicate)] == [
            str(permanent.id)
        ]
        assert all("_ruvector_lifecycle" not in metadata for metadata in seen)
        current[0] = 105.0
        assert store.scan(predicate=lambda m: m["tenant"] == "a") == []
        assert len(store.scan()) == 1


def test_restart_purge_feedback_and_compaction(tmp_path):
    current = [100.0]
    path = tmp_path / "store"
    expired, live = node(), node("b")
    expired_id, live_id = str(expired.id), str(live.id)
    with make(path, lambda: current[0]) as store:
        store.add_nodes([expired], ttl_seconds=5)
        store.add_nodes([live])
        store.record_feedback("context", expired_id, 1, "event")
    current[0] = 106.0
    with make(path, lambda: current[0]) as store:
        assert store.get(expired_id) is None
        assert store.get(live_id).metadata == live.metadata
        assert store.purge_expired() == 1
        assert store.feedback("context", [expired_id]) == {}
        assert store.purge_expired() == 0
        store.compact()
    current[0] = 100.0
    with make(path, lambda: current[0]) as store:
        assert store.get(expired_id) is None
        assert expired_id not in store._records
        assert not store.has_feedback("context")
        assert len(store) == 1


def test_compaction_default_purges_and_reclaims_capacity(tmp_path):
    current = [100.0]
    with make(tmp_path / "store", lambda: current[0], capacity=1) as store:
        store.add_nodes([node()], ttl_seconds=1)
        current[0] = 102.0
        with pytest.raises(ValueError, match="capacity"):
            store.add_nodes([node()])
        store.compact()
        store.add_nodes([node()])
        assert len(store) == 1


@pytest.mark.parametrize("ttl", [0, -1, math.inf, math.nan, True, "1", 10**1000])
def test_invalid_ttl_is_atomic(tmp_path, ttl):
    with make(tmp_path / "store", lambda: 100.0) as store:
        with pytest.raises(ValueError):
            store.add_nodes([node()], ttl_seconds=ttl)
        assert len(store) == 0


def test_reserved_metadata_and_whole_batch_validation(tmp_path):
    n = node()
    n.metadata["_ruvector_lifecycle"] = {"version": 1, "expires_at": 1}
    with make(tmp_path / "store", lambda: 100.0) as store:
        with pytest.raises(ValueError, match="reserved"):
            store.add_nodes([node(), n], ttl_seconds=1)
        assert len(store) == 0


@pytest.mark.parametrize("value", [math.inf, math.nan, True, "100", 10**1000])
def test_invalid_clock_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="clock"):
        make(tmp_path / "store", lambda: value)


async def test_async_expiry_and_purge(tmp_path):
    current = [100.0]
    with make(tmp_path / "store", lambda: current[0]) as store:
        await store.aadd_nodes([node()], ttl_seconds=1)
        current[0] = 101.0
        assert await store.asearch([1, 0, 0]) == []
        assert await store.apurge_expired() == 1
        await store.acompact()


def test_one_captured_clock_per_search(tmp_path):
    current = [100.0]
    calls = []

    def clock():
        calls.append(current[0])
        return current[0]

    with make(tmp_path / "store", clock) as store:
        store.add_nodes([node(), node()], ttl_seconds=5)
        calls.clear()
        assert len(store.search([1, 0, 0])) == 2
        assert calls == [100.0]


def test_search_fast_path_and_expiry_cache_after_delete(tmp_path, monkeypatch):
    from lionagi.integrations.ruvector.store import RuVectorStore

    current = [100.0]
    original = RuVectorStore.search
    predicates = []

    def capture(self, vector, k=5, *, predicate=None):
        predicates.append(predicate)
        return original(self, vector, k, predicate=predicate)

    monkeypatch.setattr(RuVectorStore, "search", capture)
    with make(tmp_path / "store", lambda: current[0]) as store:
        expired, live = node(), node()
        store.add_nodes([expired], ttl_seconds=1)
        store.add_nodes([live])
        store.search([1, 0, 0])
        assert predicates[-1] is None
        current[0] = 101.0
        assert [h.node_id for h in store.search([1, 0, 0], k=1)] == [str(live.id)]
        assert predicates[-1] is not None
        store.delete([str(expired.id)])
        store.search([1, 0, 0])
        assert predicates[-1] is None


def test_compact_can_retain_expired_physical_records(tmp_path):
    current = [100.0]
    path = tmp_path / "store"
    n = node()
    with make(path, lambda: current[0]) as store:
        store.add_nodes([n], ttl_seconds=1)
        current[0] = 101.0
        store.compact(purge_expired=False)
        assert str(n.id) in store._records
        assert store.get(str(n.id)) is None
    with make(path, lambda: current[0]) as store:
        assert str(n.id) in store._records
        assert store.get(str(n.id)) is None


def test_runtime_invalid_clock_cannot_purge(tmp_path):
    current = [100.0]
    with make(tmp_path / "store", lambda: current[0]) as store:
        n = node()
        store.add_nodes([n], ttl_seconds=1)
        current[0] = math.nan
        with pytest.raises(ValueError, match="clock"):
            store.purge_expired()
        assert str(n.id) in store._records


def test_purge_exceeds_public_batch_limit_atomically(tmp_path):
    current = [100.0]
    path = tmp_path / "store"
    with make(path, lambda: current[0], capacity=8, max_batch_size=2) as store:
        for _ in range(3):
            store.add_nodes([node(), node()], ttl_seconds=1)
        keep = node()
        store.add_nodes([keep])
        current[0] = 101.0
        assert store.purge_expired() == 6
        assert set(store._records) == {str(keep.id)}
    with make(path, lambda: current[0], capacity=8, max_batch_size=2) as store:
        assert len(store) == 1
        assert store.get(str(keep.id)) is not None


def test_backward_clock_fails_closed_without_resurrection(tmp_path):
    current = [100.0]
    with make(tmp_path / "store", lambda: current[0]) as store:
        n = node()
        store.add_nodes([n], ttl_seconds=1)
        current[0] = 102.0
        assert store.get(str(n.id)) is None
        current[0] = 100.0
        for action in (
            lambda: store.get(str(n.id)),
            store.scan,
            lambda: store.search([1, 0, 0]),
            store.purge_expired,
        ):
            with pytest.raises(ValueError, match="backwards"):
                action()
        assert str(n.id) in store._records
        current[0] = 103.0
        assert store.get(str(n.id)) is None
        assert store.purge_expired() == 1
