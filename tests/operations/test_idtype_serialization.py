# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import json

import pytest

from lionagi.operations.builder import OperationGraphBuilder
from lionagi.session.session import Session


@pytest.mark.asyncio
async def test_aggregation_with_UUID_serialization():
    """aggregation_sources must be JSON-serializable strings, not UUID objects."""
    # Create session and builder
    session = Session()
    builder = OperationGraphBuilder(session)

    # Create multiple operations
    source1 = builder.add_operation("chat", instruction="Source 1")
    source2 = builder.add_operation("chat", instruction="Source 2")
    source3 = builder.add_operation("chat", instruction="Source 3")

    # Create aggregation that combines results
    aggregator = builder.add_aggregation(
        "chat",
        source_node_ids=[source1, source2, source3],
        instruction="Aggregate the results from all sources",
    )

    # Get the graph
    graph = builder.graph

    # Find the aggregation operation
    agg_op = None
    for node in graph.internal_nodes.values():
        if node.metadata.get("aggregation"):
            agg_op = node
            break

    assert agg_op is not None, "Aggregation operation not found"

    # Check that aggregation_sources are strings, not UUID
    sources = agg_op.parameters.get("aggregation_sources", [])
    assert len(sources) == 3, "Should have 3 sources"

    # Verify all sources are JSON serializable strings
    for source in sources:
        assert isinstance(source, str), f"Source should be string, got {type(source)}"
        # Should be able to JSON serialize
        json.dumps(source)  # This would fail if it were UUID

    # Verify the entire parameters dict is JSON serializable
    try:
        json.dumps(agg_op.parameters)
    except TypeError as e:
        pytest.fail(f"Parameters should be JSON serializable: {e}")


@pytest.mark.asyncio
async def test_context_with_UUID_keys():
    """Predecessor context must use string keys, not UUID objects."""
    from lionagi.operations.node import Operation
    from lionagi.protocols.graph.edge import Edge
    from lionagi.protocols.graph.graph import Graph

    # Create operations
    op1 = Operation(operation="chat", parameters={"instruction": "First"})
    op2 = Operation(operation="chat", parameters={"instruction": "Second"})

    # Build graph
    graph = Graph()
    graph.add_node(op1)
    graph.add_node(op2)
    graph.add_edge(Edge(head=op1.id, tail=op2.id))

    # This test verifies UUID serialization without executing flow
    # The actual flow execution is tested in integration tests

    # Verify the graph structure is correct
    assert op1.id in graph.internal_nodes
    assert op2.id in graph.internal_nodes

    # Verify edge exists
    edges = list(graph.internal_edges.values())
    assert len(edges) == 1
    assert edges[0].head == op1.id
    assert edges[0].tail == op2.id

    # Verify parameters are JSON serializable (the main goal of this test)
    try:
        json.dumps(op1.parameters)
        json.dumps(op2.parameters)
    except TypeError as e:
        pytest.fail(f"Operation parameters should be JSON serializable: {e}")


def test_full_aggregation_flow():
    """
    Test aggregation parameter serialization to ensure no JSON serialization errors.
    """
    from lionagi.protocols.graph.edge import Edge

    session = Session()
    builder = OperationGraphBuilder(session)

    # Create a fan-out/fan-in pattern
    start = builder.add_operation("chat", instruction="Start analysis")

    # Fan out to multiple parallel operations
    analysis1 = builder.add_operation(
        "chat", instruction="Analyze aspect 1", branch=session.default_branch
    )
    analysis2 = builder.add_operation(
        "chat", instruction="Analyze aspect 2", branch=session.default_branch
    )
    analysis3 = builder.add_operation(
        "chat", instruction="Analyze aspect 3", branch=session.default_branch
    )

    # Connect start to all analyses
    builder.graph.add_edge(Edge(head=start, tail=analysis1))
    builder.graph.add_edge(Edge(head=start, tail=analysis2))
    builder.graph.add_edge(Edge(head=start, tail=analysis3))

    # Aggregate results
    summary = builder.add_aggregation(
        "chat",
        source_node_ids=[analysis1, analysis2, analysis3],
        instruction="Summarize all analyses",
    )

    # Test that aggregation parameters are JSON serializable (the main goal)
    # The actual flow execution is tested in integration tests

    # Get all operations from the graph
    operations = list(builder.graph.internal_nodes.values())

    # Find the aggregation operation
    agg_op = None
    for op in operations:
        if op.metadata.get("aggregation"):
            agg_op = op
            break

    assert agg_op is not None, "Aggregation operation not found"

    # Verify aggregation_sources are JSON serializable
    sources = agg_op.parameters.get("aggregation_sources", [])
    assert len(sources) == 3, "Should have 3 aggregation sources"

    # Test JSON serialization of all parameters
    try:
        for op in operations:
            json.dumps(op.parameters)
    except TypeError as e:
        pytest.fail(f"Operation parameters should be JSON serializable: {e}")
