"""Policy gates run against native RVF and deterministic threshold fixtures."""

import dataclasses

import pytest

pytest.importorskip("ruvector")

from lionagi import Node
from lionagi.integrations.ruvector import evaluation
from lionagi.integrations.ruvector.evaluation import (
    EvaluationCase,
    EvaluationGate,
    GovernedFeedbackRanker,
)
from lionagi.integrations.ruvector.learning import FeedbackRanker
from lionagi.integrations.ruvector.persistent import PersistentRuVectorStore
from lionagi.integrations.ruvector.store import SearchHit


@pytest.fixture
def stored(tmp_path):
    store = PersistentRuVectorStore(tmp_path / "gate.rvf", 2, embedding_id="gate-test")
    nodes = [
        Node(content="near", embedding=[1, 0], metadata={"scope": "a"}),
        Node(content="preferred", embedding=[0.995, 0.1], metadata={"scope": "b"}),
    ]
    ids = store.add_nodes(nodes)
    yield store, ids
    store.close()


@pytest.fixture
def clock(monkeypatch):
    counter = iter(range(0, 100000000, 1000))
    monkeypatch.setattr(evaluation.time, "perf_counter_ns", lambda: next(counter))


def cases_for(ids, n=20):
    return [EvaluationCase("good", (1, 0), (ids[1],)) for _ in range(n)]


def controlled(monkeypatch, ids):
    def search(self, vector, *, query, k=5, predicate=None, use_feedback=True):
        selected = ids[1] if use_feedback and self.max_boost else ids[0]
        return [SearchHit(selected, "", {}, 0)]

    monkeypatch.setattr(FeedbackRanker, "search", search)


def test_native_rvf_quality_promotion_and_rollback(stored):
    store, ids = stored
    candidate = FeedbackRanker(store)
    candidate.feedback("good", ids[1], 1, event_id="approved")
    governed = GovernedFeedbackRanker(store)
    assert governed.search([1, 0], query="good", k=1)[0].node_id == ids[0]
    report = governed.promote(
        candidate,
        cases_for(ids),
        EvaluationGate(max_p95_ratio=100),
        k=1,
    )
    assert report.accepted
    assert report.candidate_mrr == 1 and report.baseline_mrr == 0
    assert governed.search([1, 0], query="good", k=1)[0].node_id == ids[1]
    assert governed.rollback()
    assert governed.search([1, 0], query="good", k=1)[0].node_id == ids[0]
    assert not governed.rollback()


def test_thresholds_configuration_provenance_and_immutability(stored, monkeypatch, clock):
    store, ids = stored
    controlled(monkeypatch, ids)
    report = EvaluationGate().evaluate(FeedbackRanker(store), cases_for(ids), k=1)
    assert report.accepted and report.cases == 20
    assert report.embedding_id == "gate-test"
    assert len(report.dataset_digest) == 64
    assert dict(report.configuration)["max_boost"] == 0.1
    assert dict(report.gate_configuration)["min_cases"] == 20
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.accepted = False


def test_rejected_candidate_retains_previous_configuration(stored, monkeypatch, clock):
    store, ids = stored
    controlled(monkeypatch, ids)
    governed = GovernedFeedbackRanker(store)
    candidate = FeedbackRanker(store)
    assert governed.promote(candidate, cases_for(ids), k=1).accepted
    candidate.max_boost = 0
    rejected = governed.promote(candidate, cases_for(ids), k=1)
    assert not rejected.accepted
    assert "insufficient_quality_improvement" in rejected.reasons
    assert governed.search([1, 0], query="good", k=1)[0].node_id == ids[1]
    governed.rollback()
    assert governed.search([1, 0], query="good", k=1)[0].node_id == ids[0]


def test_zero_individual_regression_gate(stored, monkeypatch, clock):
    store, ids = stored
    controlled(monkeypatch, ids)
    cases = cases_for(ids, 2) + [EvaluationCase("bad", (1, 0), (ids[0],))]
    candidate = FeedbackRanker(store)
    report = EvaluationGate(min_cases=3).evaluate(candidate, cases, k=1)
    assert report.candidate_mrr > report.baseline_mrr
    assert report.regressions == 1
    assert not report.accepted and "case_quality_regression" in report.reasons
    assert (
        EvaluationGate(min_cases=3, allow_case_regressions=True)
        .evaluate(
            candidate,
            cases,
            k=1,
        )
        .accepted
    )


def test_latency_gate_rejects_and_preserves_baseline(stored, monkeypatch):
    store, ids = stored
    controlled(monkeypatch, ids)
    # Four clock calls per pair, baseline 1 ms, candidate 2 ms; alternate order.
    ticks = []
    current = 0
    for i in range(20):
        for duration in (1000000, 2000000) if i % 2 == 0 else (2000000, 1000000):
            ticks.extend([current, current + duration])
            current += duration + 100
    timer = iter(ticks)
    monkeypatch.setattr(evaluation.time, "perf_counter_ns", lambda: next(timer))
    governed = GovernedFeedbackRanker(store)
    report = governed.promote(FeedbackRanker(store), cases_for(ids), k=1)
    assert report.baseline_p95_ms == 1 and report.candidate_p95_ms == 2
    assert "latency_regression" in report.reasons
    assert governed.search([1, 0], query="good", k=1)[0].node_id == ids[0]


def test_minimum_cases_no_promotion(stored, monkeypatch, clock):
    store, ids = stored
    controlled(monkeypatch, ids)
    report = EvaluationGate().evaluate(FeedbackRanker(store), cases_for(ids, 1), k=1)
    assert not report.accepted and "insufficient_cases" in report.reasons


def test_ineligible_and_missing_labels_fail_validation(stored):
    store, ids = stored
    ranker = FeedbackRanker(store)
    with pytest.raises(ValueError, match="ineligible"):
        EvaluationGate().evaluate(
            ranker, [EvaluationCase("good", (1, 0), (ids[1],), lambda md: md["scope"] == "a")]
        )
    with pytest.raises(ValueError, match="missing"):
        EvaluationGate().evaluate(ranker, [EvaluationCase("good", (1, 0), ("missing",))])
    with pytest.raises(ValueError, match="dimensions"):
        EvaluationGate().evaluate(ranker, [EvaluationCase("good", (1,), (ids[0],))])


def test_reentrant_feedback_rejected_and_guard_restored(stored):
    store, ids = stored
    ranker = FeedbackRanker(store)

    def predicate(md):
        ranker.feedback("good", ids[1], 1, event_id="bad-predicate")
        return True

    with pytest.raises(RuntimeError, match="evaluation"):
        EvaluationGate().evaluate(
            ranker,
            [
                EvaluationCase("good", (1, 0), (ids[1],), predicate),
            ],
        )
    assert not store.has_feedback(ranker._context("good"))
    assert ranker.feedback("good", ids[1], 1, event_id="legitimate")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_cases": 0},
        {"min_cases": True},
        {"max_p95_ratio": 0.9},
        {"min_relative_improvement": float("nan")},
        {"allow_case_regressions": 1},
    ],
)
def test_invalid_gate(kwargs):
    with pytest.raises(ValueError):
        EvaluationGate(**kwargs)


@pytest.mark.parametrize(
    "vector,ids",
    [
        ((), ("a",)),
        ((float("inf"),), ("a",)),
        ((True,), ("a",)),
        ((1,), ()),
        ((1,), ("a", "a")),
    ],
)
def test_invalid_case(vector, ids):
    with pytest.raises(ValueError):
        EvaluationCase("query", vector, ids)


def test_bounds_and_candidate_identity(stored):
    store, ids = stored
    ranker = FeedbackRanker(store)
    gate = EvaluationGate()
    with pytest.raises(ValueError):
        gate.evaluate(ranker, [])
    with pytest.raises(ValueError):
        gate.evaluate(ranker, cases_for(ids, 1001))
    with pytest.raises(ValueError):
        gate.evaluate(ranker, cases_for(ids), k=store.max_k + 1)
    governed = GovernedFeedbackRanker(store)
    with pytest.raises(ValueError):
        governed.promote(governed, cases_for(ids))


@pytest.mark.asyncio
async def test_async_promotion_and_search(stored, monkeypatch, clock):
    store, ids = stored
    controlled(monkeypatch, ids)
    governed = GovernedFeedbackRanker(store)
    assert (await governed.apromote(FeedbackRanker(store), cases_for(ids), k=1)).accepted
    hits = await governed.asearch([1, 0], query="good", k=1)
    assert hits[0].node_id == ids[1]


def test_lifecycle_epoch_frozen_across_paired_searches(tmp_path, monkeypatch, clock):
    from lionagi.integrations.ruvector.lifecycle import LifecycleRuVectorStore

    epoch = [100.0]
    with LifecycleRuVectorStore(
        tmp_path / "expiry-evaluation",
        2,
        embedding_id="expiry-test",
        clock=lambda: epoch[0],
    ) as store:
        first = Node(content="near", embedding=[1, 0])
        preferred = Node(content="preferred", embedding=[0.995, 0.1])
        store.add_nodes([first])
        ids = store.add_nodes([preferred], ttl_seconds=10)
        candidate = FeedbackRanker(store)
        candidate.feedback("good", ids[0], 1, event_id="approved")
        epoch[0] = 109.0
        original = store.search

        def advancing_search(vector, **kwargs):
            epoch[0] += 5
            return original(vector, **kwargs)

        monkeypatch.setattr(store, "search", advancing_search)
        report = EvaluationGate(min_cases=1).evaluate(
            candidate,
            [EvaluationCase("good", (1, 0), tuple(ids))],
            k=1,
        )
        assert report.accepted
        assert report.baseline_mrr == 0 and report.candidate_mrr == 1
        assert not store._evaluation_active
        assert store._evaluation_time is None
        # After evaluation, the real clock resumes and the record is expired.
        assert store.get(ids[0]) is None


@pytest.mark.parametrize(
    "vector,ids",
    [
        ("12", ("a",)),
        ((1,), "a"),
        ((10**1000,), ("a",)),
        ((1,), tuple("x" for _ in range(1001))),
        ((1,) * 8193, ("a",)),
    ],
)
def test_malformed_or_oversize_cases_rejected_before_materialization(vector, ids):
    with pytest.raises(ValueError):
        EvaluationCase("query", vector, ids)


def test_failed_vector_validation_restores_guard_without_feedback_change(stored):
    store, ids = stored
    ranker = FeedbackRanker(store)
    with pytest.raises(ValueError):
        EvaluationGate().evaluate(ranker, [EvaluationCase("query", (0, 0), (ids[0],))])
    assert not store._evaluation_active
    assert not store.has_feedback(ranker._context("query"))
    assert ranker.feedback("query", ids[1], 1, event_id="after-invalid")


def test_governed_search_rejects_nonboolean_switch(stored):
    store, _ = stored
    with pytest.raises(ValueError, match="boolean"):
        GovernedFeedbackRanker(store).search([1, 0], query="query", use_feedback=1)
