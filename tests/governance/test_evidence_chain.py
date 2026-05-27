# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import time

import pytest

from lionagi.protocols.generic.log import DataLogger
from lionagi.protocols.generic.pile import Pile, PileAppendOnlyError
from lionagi.protocols.governance.evidence import (
    GENESIS_HASH,
    ChainVerification,
    EvidenceChain,
    EvidenceNode,
    LogTier,
    compute_node_hash,
)

# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_happy_path_verify_chain_passes():
    chain = EvidenceChain()
    n1 = chain.append({"a": 1})
    n2 = chain.append({"b": 2})

    result = chain.verify()
    assert result.valid is True
    assert result.checked_count == 2
    assert chain.head_hash() == n2.node_hash
    assert n1.previous_hash == GENESIS_HASH
    assert n2.previous_hash == n1.node_hash


def test_happy_path_empty_chain_is_valid():
    chain = EvidenceChain()
    result = chain.verify()
    assert result.valid is True
    assert result.checked_count == 0


# ---------------------------------------------------------------------------
# 2. Tamper detection
# ---------------------------------------------------------------------------


def test_tamper_detection():
    chain = EvidenceChain()
    node = chain.append({"a": 1})
    # Mutate the inner dict — frozen model allows this (dict mutation, not reassignment)
    node.content["a"] = 99
    result = chain.verify()
    assert result.valid is False
    assert result.first_invalid_index == 0
    assert result.violation_type == "tamper"


# ---------------------------------------------------------------------------
# 3. Reorder detection
# ---------------------------------------------------------------------------


def test_reorder_detection():
    chain = EvidenceChain()
    chain.append({"a": 1})
    chain.append({"b": 2})

    # Simulate adversarial reorder by manipulating internals
    ids = list(chain.nodes.progression.order)
    chain.nodes.progression.order.clear()
    chain.nodes.progression.order.extend(reversed(ids))
    chain.nodes.progression._rebuild_members()

    result = chain.verify()
    assert result.valid is False
    assert result.violation_type == "reorder"


# ---------------------------------------------------------------------------
# 4. Delete detection
# ---------------------------------------------------------------------------


def test_delete_detection():
    chain = EvidenceChain()
    chain.append({"a": 1})
    chain.append({"b": 2})

    # Simulate storage tampering by removing a node via internals
    victim_id = list(chain.nodes.progression.order)[0]
    victim = chain.nodes.collections[victim_id]
    chain.nodes.collections.pop(victim.id)
    chain.nodes.progression.exclude(victim.id)

    result = chain.verify()
    assert result.valid is False
    assert result.violation_type == "delete"


# ---------------------------------------------------------------------------
# 5. SHA-256 determinism
# ---------------------------------------------------------------------------


def test_sha256_determinism_key_order_independent():
    h1 = compute_node_hash({"a": 1, "b": 2}, GENESIS_HASH)
    h2 = compute_node_hash({"b": 2, "a": 1}, GENESIS_HASH)
    assert h1 == h2


def test_sha256_determinism_same_content_same_hash():
    node1 = EvidenceNode(content={"x": 1}, previous_hash=GENESIS_HASH)
    node2 = EvidenceNode(content={"x": 1}, previous_hash=GENESIS_HASH)
    assert node1.node_hash == node2.node_hash


def test_sha256_determinism_different_content_different_hash():
    h1 = compute_node_hash({"a": 1}, GENESIS_HASH)
    h2 = compute_node_hash({"a": 2}, GENESIS_HASH)
    assert h1 != h2


# ---------------------------------------------------------------------------
# 6. Microbenchmark: hashing under 1ms per 1KB payload
# ---------------------------------------------------------------------------


def test_hashing_under_1ms_per_1kb_payload():
    payload = {"data": "x" * 1024}
    iterations = 1000
    start = time.perf_counter()
    for _ in range(iterations):
        compute_node_hash(payload, GENESIS_HASH)
    elapsed_ms = (time.perf_counter() - start) * 1000 / iterations
    assert elapsed_ms < 1.0, f"Hash took {elapsed_ms:.4f}ms, expected < 1ms"


# ---------------------------------------------------------------------------
# 7. Append-only Pile
# ---------------------------------------------------------------------------


def test_append_only_pile_mutation_raises():
    item1 = EvidenceNode(content={"n": 1})
    item2 = EvidenceNode(content={"n": 2})
    item3 = EvidenceNode(content={"n": 3})

    pile = Pile(collections=[item1], item_type={EvidenceNode}, append_only=True)

    # Append of a new item must succeed
    pile.include(item2)
    assert item2 in pile

    # Mutation / deletion operations must raise
    with pytest.raises(PileAppendOnlyError):
        pile[0] = item3

    with pytest.raises(PileAppendOnlyError):
        pile.pop(item1.id)

    with pytest.raises(PileAppendOnlyError):
        pile.remove(item1)

    with pytest.raises(PileAppendOnlyError):
        pile.clear()

    with pytest.raises(PileAppendOnlyError):
        pile.insert(0, item3)


def test_append_only_pile_include_existing_raises():
    item1 = EvidenceNode(content={"n": 1})
    pile = Pile(collections=[item1], item_type={EvidenceNode}, append_only=True)

    with pytest.raises(PileAppendOnlyError):
        pile.include(item1)


def test_normal_pile_not_affected():
    item1 = EvidenceNode(content={"n": 1})
    item2 = EvidenceNode(content={"n": 2})
    pile = Pile(collections=[item1], item_type={EvidenceNode}, append_only=False)
    pile.pop(item1.id)
    assert item1 not in pile
    pile.include(item2)
    assert item2 in pile


# ---------------------------------------------------------------------------
# 8. Tier routing
# ---------------------------------------------------------------------------


def test_tier_routing_mutable():
    logger = DataLogger(auto_save_on_exit=False)
    record = logger.emit({"m": 1}, LogTier.MUTABLE)
    assert record in logger.logs
    assert record not in logger.protected_logs


def test_tier_routing_protected():
    logger = DataLogger(auto_save_on_exit=False)
    record = logger.emit({"p": 1}, LogTier.PROTECTED)
    assert record in logger.protected_logs
    assert record not in logger.logs


def test_tier_routing_immutable():
    logger = DataLogger(auto_save_on_exit=False)
    record = logger.emit({"i": 1}, LogTier.IMMUTABLE)
    assert record in logger.immutable_logs
    assert record not in logger.logs
    assert record not in logger.protected_logs
    assert logger.verify_immutable().valid is True


def test_tier_routing_all_three():
    logger = DataLogger(auto_save_on_exit=False)
    mutable = logger.emit({"m": 1}, LogTier.MUTABLE)
    protected = logger.emit({"p": 1}, LogTier.PROTECTED)
    immutable = logger.emit({"i": 1}, LogTier.IMMUTABLE)

    assert mutable in logger.logs
    assert protected in logger.protected_logs
    assert immutable in logger.immutable_logs
    assert logger.verify_immutable().valid is True


# ---------------------------------------------------------------------------
# 9. Sensitive field exclusion
# ---------------------------------------------------------------------------


def test_sensitive_field_excluded_from_node_content():
    logger = DataLogger(auto_save_on_exit=False)
    node = logger.emit(
        {"visible": "ok", "secret": "no"},
        LogTier.IMMUTABLE,
        sensitive_fields=["secret"],
    )
    assert "secret" not in node.content
    assert "visible" in node.content


def test_sensitive_field_excluded_from_display_content():
    chain = EvidenceChain()
    node = chain.append(
        {"visible": "ok", "secret": "no"},
        sensitive_fields=["secret"],
    )
    assert "secret" not in node.display_content()
    assert "visible" in node.display_content()


def test_sensitive_field_excluded_from_audit_dict():
    chain = EvidenceChain()
    node = chain.append(
        {"visible": "ok", "secret": "no"},
        sensitive_fields=["secret"],
    )
    audit = node.to_audit_dict()
    assert "secret" not in str(audit)


# ---------------------------------------------------------------------------
# 10. Branch mount
# ---------------------------------------------------------------------------


def test_branch_evidence_chain_lazy_init():
    from lionagi.session.branch import Branch

    branch = Branch()
    assert branch.evidence_chain is None

    node = branch.emit_evidence({"event": "x"})
    assert branch.evidence_chain is not None
    assert branch.evidence_chain.verify().valid is True
    assert branch.metadata["evidence_chain_tip"] == node.node_hash


def test_branch_evidence_chain_explicit_mount():
    from lionagi.session.branch import Branch

    chain = EvidenceChain()
    branch = Branch(evidence_chain=chain)
    assert branch.evidence_chain is chain

    node = branch.emit_evidence({"event": "y"})
    assert chain.verify().valid is True
    assert branch.metadata["evidence_chain_tip"] == node.node_hash


def test_branch_evidence_default_tier_is_immutable():
    from lionagi.session.branch import Branch

    branch = Branch()
    node = branch.emit_evidence({"k": "v"})
    assert node.tier == LogTier.IMMUTABLE.value or node.tier == LogTier.IMMUTABLE


# ---------------------------------------------------------------------------
# 11. ChainVerification fields
# ---------------------------------------------------------------------------


def test_chain_verification_fields_on_valid():
    chain = EvidenceChain()
    chain.append({"a": 1})
    result = chain.verify()
    assert isinstance(result, ChainVerification)
    assert result.valid is True
    assert result.first_invalid_index is None
    assert result.violation_type is None


def test_chain_verification_fields_on_tamper():
    chain = EvidenceChain()
    node = chain.append({"a": 1})
    node.content["a"] = 0
    result = chain.verify()
    assert result.valid is False
    assert result.first_invalid_index == 0
    assert result.violation_type == "tamper"
    assert result.expected_hash is not None
    assert result.actual_hash is not None
