# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading

import pytest

from lionagi.governance.evidence import (
    GENESIS_HASH,
    ChainVerifier,
    EvidenceChain,
    EvidenceNode,
    LogTier,
    compute_node_hash,
)


class TestEvidenceNode:
    def test_hash_computed_on_creation(self):
        node = EvidenceNode(content={"k": "v"}, previous_hash=GENESIS_HASH)
        assert node.node_hash != ""
        assert len(node.node_hash) == 64

    def test_verify_hash_passes(self):
        node = EvidenceNode(content={"x": 1}, previous_hash=GENESIS_HASH)
        assert node.verify_hash()

    def test_genesis_hash_is_zeros(self):
        assert GENESIS_HASH == "0" * 64

    def test_compute_node_hash_deterministic(self):
        h1 = compute_node_hash({"a": 1}, GENESIS_HASH)
        h2 = compute_node_hash({"a": 1}, GENESIS_HASH)
        assert h1 == h2

    def test_compute_node_hash_changes_with_content(self):
        h1 = compute_node_hash({"a": 1}, GENESIS_HASH)
        h2 = compute_node_hash({"a": 2}, GENESIS_HASH)
        assert h1 != h2

    def test_compute_node_hash_changes_with_prev(self):
        h1 = compute_node_hash({"a": 1}, GENESIS_HASH)
        h2 = compute_node_hash({"a": 1}, "1" * 64)
        assert h1 != h2


class TestEvidenceChain:
    def test_empty_chain_verifies(self):
        chain = EvidenceChain()
        v = chain.verify()
        assert v.valid
        assert v.checked_count == 0

    def test_append_single_node(self):
        chain = EvidenceChain()
        node = chain.append({"event": "test"})
        assert chain.node_count == 1
        assert chain.tip_hash == node.node_hash
        assert chain.nodes()[0] is node

    def test_chain_links_correctly(self):
        chain = EvidenceChain()
        n1 = chain.append({"seq": 1})
        n2 = chain.append({"seq": 2})
        assert n2.previous_hash == n1.node_hash

    def test_verify_multi_node_chain(self):
        chain = EvidenceChain()
        for i in range(5):
            chain.append({"i": i})
        v = chain.verify()
        assert v.valid
        assert v.checked_count == 5

    def test_head_hash_matches_last_node(self):
        chain = EvidenceChain()
        nodes = [chain.append({"n": i}) for i in range(3)]
        assert chain.head_hash() == nodes[-1].node_hash

    def test_nodes_returns_copy(self):
        chain = EvidenceChain()
        chain.append({"x": 1})
        snapshot = chain.nodes()
        chain.append({"x": 2})
        assert len(snapshot) == 1  # snapshot is not affected by later appends

    def test_log_tier_stored(self):
        chain = EvidenceChain()
        node = chain.append({"e": "mutable"}, tier=LogTier.MUTABLE)
        assert node.tier == LogTier.MUTABLE.value

    def test_concurrent_appends_are_safe(self):
        chain = EvidenceChain()
        errors: list[Exception] = []

        def worker():
            try:
                for i in range(20):
                    chain.append({"i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert chain.node_count == 100
        v = chain.verify()
        assert v.valid


class TestChainVerifier:
    def test_detects_count_mismatch(self):
        chain = EvidenceChain()
        chain.append({"x": 1})
        # Manually corrupt the count
        object.__setattr__(chain, "node_count", 99)
        v = ChainVerifier.verify(chain)
        assert not v.valid
        assert v.violation_type == "delete"

    def test_detects_tampered_node(self):
        chain = EvidenceChain()
        chain.append({"x": 1})
        nodes = chain.nodes()
        # Tamper: rebuild a node with wrong hash stored
        bad_node = EvidenceNode(
            content={"x": 1},
            previous_hash=GENESIS_HASH,
            node_hash="bad" + "0" * 61,  # 64 chars, wrong
        )
        chain._nodes[0] = bad_node  # type: ignore[index]
        v = chain.verify()
        assert not v.valid
        assert v.violation_type == "tamper"
