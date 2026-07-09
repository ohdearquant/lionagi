# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0101 D4: the capability-class declarative map, pure unit tests (no DB)."""

from __future__ import annotations

from lionagi.studio.scheduler import capabilities


def test_unknown_token_defaults_to_eligibility():
    assert capabilities.capability_class("some-new-token") == "eligibility"


def test_gpu_exclusive_is_serialization():
    assert capabilities.capability_class("gpu-exclusive") == "serialization"


def test_warmed_cache_is_affinity():
    assert capabilities.capability_class("warmed-cache") == "affinity"


def test_matching_tokens_excludes_affinity():
    tokens = ["lean-toolchain", "gpu-exclusive", "warmed-cache"]
    assert set(capabilities.matching_tokens(tokens)) == {"lean-toolchain", "gpu-exclusive"}


def test_worker_can_serve_subset_match_positive():
    assert capabilities.worker_can_serve(["lean-toolchain"], ["lean-toolchain", "other"]) is True


def test_worker_can_serve_subset_match_negative():
    assert (
        capabilities.worker_can_serve(["lean-toolchain", "gpu-exclusive"], ["lean-toolchain"])
        is False
    )


def test_worker_can_serve_ignores_affinity_tokens():
    # A worker lacking the affinity token can still serve -- affinity never filters.
    assert capabilities.worker_can_serve(["warmed-cache"], []) is True


def test_worker_can_serve_empty_requirements_always_true():
    assert capabilities.worker_can_serve([], []) is True
    assert capabilities.worker_can_serve(None, None) is True


def test_host_scoped_concurrency_key_only_serialization_tokens():
    key = capabilities.host_scoped_concurrency_key(
        "myhost", ["gpu-exclusive", "lean-toolchain", "warmed-cache"]
    )
    assert key == "myhost:gpu-exclusive"


def test_host_scoped_concurrency_key_none_without_serialization_token():
    assert (
        capabilities.host_scoped_concurrency_key("myhost", ["lean-toolchain", "warmed-cache"])
        is None
    )
    assert capabilities.host_scoped_concurrency_key("myhost", []) is None
    assert capabilities.host_scoped_concurrency_key("myhost", None) is None


def test_host_scoped_concurrency_key_sorted_and_joined_for_multiple_serialization_tokens(
    monkeypatch,
):
    # Two serialization tokens on one task: the key must be deterministically
    # sorted, not submission order, so both derivations of the same task
    # (retry, resubmission) collide on the identical key.
    monkeypatch.setitem(capabilities.CAPABILITY_CLASSES, "zeta-exclusive", "serialization")
    key = capabilities.host_scoped_concurrency_key("myhost", ["zeta-exclusive", "gpu-exclusive"])
    assert key == "myhost:gpu-exclusive+zeta-exclusive"


def test_affinity_score_counts_matched_affinity_tokens():
    assert capabilities.affinity_score(["warmed-cache"], ["warmed-cache"]) == 1
    assert capabilities.affinity_score(["warmed-cache"], []) == 0
    # Non-affinity tokens never contribute to the score.
    assert capabilities.affinity_score(["lean-toolchain"], ["lean-toolchain"]) == 0
