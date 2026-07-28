# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Graph-shape tests for `add_operation`'s three-valued `depends_on`.

These assert the SHAPE of the built graph, not that a run completes: a
serialized fan completes fine, it is only slow.
"""

from __future__ import annotations

from collections import Counter

from lionagi.operations.builder import ExpansionStrategy, OperationGraphBuilder


def _label_counts(builder: OperationGraphBuilder) -> Counter:
    return Counter(tuple(e.label or []) for e in builder.graph.internal_edges.values())


def test_empty_depends_on_builds_a_fan_not_a_chain():
    """`depends_on=[]` means explicitly-none: no incoming edge, whatever the heads are."""
    builder = OperationGraphBuilder("fan")
    workers = [
        builder.add_operation("operate", depends_on=[], instruction=f"w{i}") for i in range(5)
    ]
    # Positive control in the same graph: a sink that DOES declare dependencies
    # still gets them, so these assertions cannot pass on an empty graph.
    sink = builder.add_operation("operate", depends_on=workers, instruction="sink")

    counts = _label_counts(builder)
    assert counts[("sequential",)] == 0
    assert counts[("depends_on",)] == 5

    incoming = {str(e.tail) for e in builder.graph.internal_edges.values()}
    assert incoming == {str(sink)}


def test_none_depends_on_still_chains_onto_current_heads():
    """The chaining convention is not removed, only made addressable."""
    builder = OperationGraphBuilder("chain")
    first = builder.add_operation("operate", depends_on=None, instruction="one")
    second = builder.add_operation("operate", depends_on=None, instruction="two")

    counts = _label_counts(builder)
    assert counts[("sequential",)] == 1

    edge = next(iter(builder.graph.internal_edges.values()))
    assert str(edge.head) == str(first)
    assert str(edge.tail) == str(second)


def test_default_depends_on_is_none_and_still_chains():
    """Callers that omit the argument entirely keep the old behaviour."""
    builder = OperationGraphBuilder("chain")
    builder.add_operation("operate", instruction="one")
    builder.add_operation("operate", instruction="two")

    assert _label_counts(builder)[("sequential",)] == 1


def test_empty_depends_on_does_not_disturb_the_heads_for_a_later_chainer():
    """After a fan, an `add_operation(depends_on=None)` chains onto the last node only."""
    builder = OperationGraphBuilder("mixed")
    builder.add_operation("operate", depends_on=[], instruction="a")
    b = builder.add_operation("operate", depends_on=[], instruction="b")
    builder.add_operation("operate", instruction="chained")

    counts = _label_counts(builder)
    assert counts[("sequential",)] == 1
    seq = next(e for e in builder.graph.internal_edges.values() if list(e.label) == ["sequential"])
    assert str(seq.head) == str(b)


def test_expand_from_result_concurrent_fans_from_the_source_only():
    """The expansion path wires every item from the source; siblings are unrelated."""
    builder = OperationGraphBuilder("expand")
    source = builder.add_operation("operate", instruction="root")
    children = builder.expand_from_result(
        items=["a", "b", "c", "d", "e"],
        source_node_id=source,
        operation="operate",
        strategy=ExpansionStrategy.CONCURRENT,
    )
    sink = builder.add_operation("operate", depends_on=children, instruction="sink")

    counts = _label_counts(builder)
    assert counts[("sequential",)] == 0
    assert counts[("expansion", "concurrent")] == 5
    assert counts[("depends_on",)] == 5

    # Every expansion edge comes from the source, so no child waits on a sibling.
    heads = {
        str(e.head) for e in builder.graph.internal_edges.values() if "expansion" in list(e.label)
    }
    assert heads == {str(source)}
    assert str(sink) not in heads


def test_inherit_context_with_empty_depends_on_records_no_primary_dependency():
    """`inherit_context` needs a dependency to inherit from; `[]` supplies none."""
    builder = OperationGraphBuilder("inherit")
    node_id = builder.add_operation("operate", depends_on=[], inherit_context=True, instruction="w")
    node = builder._operations[node_id]
    assert "primary_dependency" not in node.metadata
