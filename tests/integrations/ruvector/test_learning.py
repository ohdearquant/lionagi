"""Bounded relevance feedback on the actual persistent Rust-backed store."""

import math

import pytest

ruvector = pytest.importorskip("ruvector")
_REQUIRED_RVF_APIS = ("set_metadata", "get_metadata", "export_vectors", "metadata_recovery")
_HAS_RVF_CHECKPOINTS = all(
    hasattr(getattr(ruvector, "RvfStore", None), name) for name in _REQUIRED_RVF_APIS
)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _HAS_RVF_CHECKPOINTS,
        reason="Installed RuVector SDK lacks required RVF checkpoint APIs; requires SDK 0.1.4",
    ),
]

from lionagi import Node
from lionagi.integrations.ruvector.learning import FeedbackRanker
from lionagi.integrations.ruvector.persistent import PersistentRuVectorStore


@pytest.fixture
def stored(tmp_path):
    store = PersistentRuVectorStore(tmp_path / "memory.rvf", 2, embedding_id="test-v1")
    a = Node(content="nearest", embedding=[1, 0], metadata={"scope": "a"})
    b = Node(content="preferred", embedding=[0.995, 0.1], metadata={"scope": "b"})
    c = Node(content="far", embedding=[0, 1], metadata={"scope": "b"})
    store.add_nodes([a, b, c])
    yield store, [str(a.id), str(b.id), str(c.id)]
    store.close()


def test_zero_feedback_and_disabled_preserve_native_k(stored, monkeypatch):
    store, ids = stored
    ranker = FeedbackRanker(store)
    calls = []
    original = store.search

    def tracked(vector, k=5, **kwargs):
        calls.append(k)
        return original(vector, k=k, **kwargs)

    monkeypatch.setattr(store, "search", tracked)
    baseline = original([1, 0], k=1)
    assert ranker.search([1, 0], query="query", k=1) == baseline
    ranker.feedback("query", ids[1], 1, event_id="one")
    assert ranker.search([1, 0], query="query", k=1, use_feedback=False) == baseline
    assert calls == [1, 1]


def test_feedback_canary_signed_reward_scope_reset(stored):
    store, ids = stored
    ranker = FeedbackRanker(store)
    assert ranker.feedback("query", ids[1], 1, event_id="one")
    assert not ranker.feedback("query", ids[1], 1, event_id="one")
    assert ranker.search([1, 0], query="query", k=1)[0].node_id == ids[1]
    assert ranker.search([1, 0], query="other", k=1)[0].node_id == ids[0]
    ranker.feedback("query", ids[1], -1, event_id="two")
    assert ranker.search([1, 0], query="query", k=1)[0].node_id == ids[0]
    assert ranker.reset("query") == 2
    assert ranker.reset("query") == 0
    assert ranker.search([1, 0], query="query", k=1)[0].node_id == ids[0]


def test_filter_cannot_be_overridden_and_distances_preserved(stored):
    store, ids = stored
    ranker = FeedbackRanker(store)
    ranker.feedback("query", ids[1], 1, event_id="one")
    hits = ranker.search([1, 0], query="query", k=3)
    original = {hit.node_id: hit.distance for hit in store.search([1, 0], k=3)}
    assert all(hit.distance == original[hit.node_id] for hit in hits)
    assert [
        h.node_id
        for h in ranker.search([1, 0], query="query", k=3, predicate=lambda md: md["scope"] == "a")
    ] == [ids[0]]


def test_saturation_cannot_promote_far_candidate(stored):
    store, ids = stored
    ranker = FeedbackRanker(store, max_boost=0.25, prior=1)
    for index in range(30):
        ranker.feedback("query", ids[2], 1, event_id=str(index))
        ranker.feedback("query", ids[0], -1, event_id=f"negative{index}")
    hits = ranker.search([1, 0], query="query", k=3)
    assert hits[-1].node_id == ids[2]
    assert all(0 <= hit.distance <= 2 for hit in hits)


@pytest.mark.parametrize(
    "options",
    [
        {"max_boost": -0.1},
        {"max_boost": 10**400},
        {"prior": 10**400},
        {"max_boost": 0.251},
        {"max_boost": math.nan},
        {"max_boost": True},
        {"prior": 0},
        {"prior": math.inf},
        {"candidate_multiplier": 0},
        {"candidate_multiplier": 17},
        {"candidate_multiplier": True},
    ],
)
def test_invalid_configuration(stored, options):
    with pytest.raises(ValueError):
        FeedbackRanker(stored[0], **options)


@pytest.mark.parametrize("reward", [-1.01, 1.01, math.nan, math.inf, True, "1"])
def test_invalid_reward(stored, reward):
    store, ids = stored
    with pytest.raises(ValueError):
        FeedbackRanker(store).feedback("query", ids[0], reward, event_id="one")


def test_query_and_search_guards(stored):
    ranker = FeedbackRanker(stored[0])
    for query in (None, "x" * (stored[0].max_content_bytes + 1)):
        with pytest.raises(ValueError):
            ranker.search([1, 0], query=query)
    with pytest.raises(ValueError):
        ranker.search([1, 0], query="query", use_feedback=1)
    with pytest.raises(ValueError):
        ranker.search([1, 0], query="query", k=True)
    with pytest.raises(AttributeError):
        ranker.store = stored[0]


async def test_async_and_feedback_survives_reopen(tmp_path):
    path = tmp_path / "memory.rvf"
    with PersistentRuVectorStore(path, 2, embedding_id="test-v1") as store:
        a = Node(content="nearest", embedding=[1, 0])
        b = Node(content="preferred", embedding=[0.995, 0.1])
        store.add_nodes([a, b])
        FeedbackRanker(store).feedback("query", str(b.id), 1, event_id="one")
    with PersistentRuVectorStore(path, 2, embedding_id="test-v1") as store:
        ranker = FeedbackRanker(store)
        assert (await ranker.asearch([1, 0], query="query", k=1))[0].node_id == str(b.id)
        assert not ranker.feedback("query", str(b.id), 1, event_id="one")


def test_zero_boost_preserves_baseline_and_context_namespaces(stored, tmp_path):
    store, ids = stored
    ranker = FeedbackRanker(store, max_boost=0)
    ranker.feedback("query", ids[1], 1, event_id="one")
    assert ranker.search([1, 0], query="query", k=1) == store.search([1, 0], k=1)
    with PersistentRuVectorStore(tmp_path / "other.rvf", 2, embedding_id="test-v2") as other:
        assert ranker._context("query") != FeedbackRanker(other)._context("query")
    assert ranker._context("query") != ranker._context("Query")
    assert "query" not in ranker._context("query")


@pytest.mark.parametrize("store", [None, object(), {}, "memory.rvf"])
def test_requires_persistent_store(store):
    with pytest.raises(TypeError, match="PersistentRuVectorStore"):
        FeedbackRanker(store)
