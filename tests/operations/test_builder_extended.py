# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Comprehensive tests for OperationGraphBuilder."""

import pytest

from lionagi.operations.builder import ExpansionStrategy, OperationGraphBuilder


class TestExpansionStrategy:
    """Test ExpansionStrategy enum."""

    def test_expansion_strategy_values(self):
        """Test all expansion strategy enum values."""
        assert ExpansionStrategy.CONCURRENT.value == "concurrent"
        assert ExpansionStrategy.SEQUENTIAL.value == "sequential"
        assert ExpansionStrategy.SEQUENTIAL_CONCURRENT_CHUNK.value == "sequential_concurrent_chunk"
        assert ExpansionStrategy.CONCURRENT_SEQUENTIAL_CHUNK.value == "concurrent_sequential_chunk"


class TestOperationGraphBuilderBasics:
    """Test basic builder initialization and operation addition."""

    def test_builder_initialization(self):
        """Test OperationGraphBuilder initialization."""
        builder = OperationGraphBuilder()
        assert builder.name == "DynamicGraph"
        assert len(builder._operations) == 0
        assert len(builder._executed) == 0
        assert builder._current_heads == []
        assert builder.last_operation_id is None

    def test_builder_initialization_with_name(self):
        """Test builder initialization with custom name."""
        builder = OperationGraphBuilder(name="CustomGraph")
        assert builder.name == "CustomGraph"

    def test_add_operation_basic(self):
        """Test adding a basic operation."""
        builder = OperationGraphBuilder()
        node_id = builder.add_operation(operation="chat", instruction="Hello world")

        assert node_id is not None
        assert node_id in builder._operations
        assert builder.last_operation_id == node_id
        assert builder._current_heads == [node_id]

        # Verify operation was added to graph
        node = builder._operations[node_id]
        assert node.operation == "chat"
        assert node.parameters["instruction"] == "Hello world"

    def test_add_operation_with_reference_id(self):
        """Test adding operation with reference ID."""
        builder = OperationGraphBuilder()
        node_id = builder.add_operation(operation="chat", node_id="ref_1", instruction="Test")

        node = builder._operations[node_id]
        assert node.metadata.get("reference_id") == "ref_1"

        # Test retrieval by reference
        retrieved = builder.get_node_by_reference("ref_1")
        assert retrieved == node

    def test_add_operation_with_branch(self):
        """Test adding operation with branch ID."""
        builder = OperationGraphBuilder()
        branch_id = "12345678-1234-5678-1234-567812345678"
        node_id = builder.add_operation(operation="chat", branch=branch_id, instruction="Test")

        node = builder._operations[node_id]
        assert str(node.branch_id) == branch_id


class TestOperationGraphBuilderDependencies:
    """Test operation dependencies and linking."""

    def test_add_operation_with_dependencies(self):
        """Test adding operation with explicit dependencies."""
        builder = OperationGraphBuilder()

        # Add first operation
        op1_id = builder.add_operation(operation="chat", instruction="First")

        # Add second operation depending on first
        op2_id = builder.add_operation(
            operation="operate", depends_on=[op1_id], instruction="Second"
        )

        # Verify edge was created
        edges = list(builder.graph.internal_edges)
        assert len(edges) == 1
        assert edges[0].head == op1_id
        assert edges[0].tail == op2_id
        assert "depends_on" in edges[0].label

    def test_auto_linking_from_current_heads(self):
        """Test automatic linking from current heads."""
        builder = OperationGraphBuilder()

        # Add operations sequentially
        op1_id = builder.add_operation(operation="chat", instruction="First")
        op2_id = builder.add_operation(operation="chat", instruction="Second")

        # Should auto-link from op1 to op2
        edges = list(builder.graph.internal_edges)
        assert len(edges) == 1
        assert edges[0].head == op1_id
        assert edges[0].tail == op2_id
        assert "sequential" in edges[0].label

    def test_inherit_context(self):
        """Test context inheritance from dependencies."""
        builder = OperationGraphBuilder()

        op1_id = builder.add_operation(operation="chat", instruction="First")
        op2_id = builder.add_operation(
            operation="chat",
            depends_on=[op1_id],
            inherit_context=True,
            instruction="Second",
        )

        node = builder._operations[op2_id]
        assert node.metadata.get("inherit_context") is True
        assert node.metadata.get("primary_dependency") == op1_id


class TestOperationGraphBuilderExpansion:
    """Test graph expansion from results."""

    def test_expand_from_result_concurrent(self):
        """Test concurrent expansion from result."""
        builder = OperationGraphBuilder()

        source_id = builder.add_operation(operation="operate", instruction="Generate ideas")

        # Mock result items
        items = ["idea1", "idea2", "idea3"]

        new_ids = builder.expand_from_result(
            items=items,
            source_node_id=source_id,
            operation="chat",
            strategy=ExpansionStrategy.CONCURRENT,
            shared_param="value",
        )

        assert len(new_ids) == 3
        assert builder._current_heads == new_ids

        # Verify all nodes were created and linked
        for i, node_id in enumerate(new_ids):
            assert node_id in builder._operations
            node = builder._operations[node_id]
            assert node.operation == "chat"
            assert node.parameters["item_index"] == i
            assert node.parameters["shared_param"] == "value"
            assert node.parameters["expanded_from"] == source_id
            assert node.metadata["expansion_source"] == source_id
            assert node.metadata["expansion_index"] == i

        # Verify edges from source to expanded nodes
        edges = list(builder.graph.internal_edges)
        assert len(edges) == 3
        for edge in edges:
            assert edge.head == source_id
            assert "expansion" in edge.label
            assert "concurrent" in edge.label

    def test_expand_from_result_sequential(self):
        """Test sequential expansion from result."""
        builder = OperationGraphBuilder()

        source_id = builder.add_operation(operation="operate", instruction="Generate ideas")

        items = ["item1", "item2"]

        new_ids = builder.expand_from_result(
            items=items,
            source_node_id=source_id,
            operation="chat",
            strategy=ExpansionStrategy.SEQUENTIAL,
        )

        assert len(new_ids) == 2
        # All new nodes should be current heads for sequential
        assert builder._current_heads == new_ids

    def test_expand_from_result_with_models(self):
        """Test expansion with Pydantic model items."""
        from pydantic import BaseModel

        class TestModel(BaseModel):
            field1: str
            field2: int

        builder = OperationGraphBuilder()
        source_id = builder.add_operation(operation="operate", instruction="Test")

        items = [
            TestModel(field1="value1", field2=1),
            TestModel(field1="value2", field2=2),
        ]

        new_ids = builder.expand_from_result(
            items=items,
            source_node_id=source_id,
            operation="chat",
            strategy=ExpansionStrategy.CONCURRENT,
        )

        # Verify model parameters were extracted
        node1 = builder._operations[new_ids[0]]
        assert node1.parameters["field1"] == "value1"
        assert node1.parameters["field2"] == 1

    def test_expand_with_inherit_context(self):
        """Test expansion with context inheritance."""
        builder = OperationGraphBuilder()
        source_id = builder.add_operation(operation="operate", instruction="Source")

        items = ["item1", "item2"]

        new_ids = builder.expand_from_result(
            items=items,
            source_node_id=source_id,
            operation="chat",
            inherit_context=True,
        )

        # First node should inherit from source
        node1 = builder._operations[new_ids[0]]
        assert node1.metadata.get("inherit_context") is True
        assert node1.metadata.get("primary_dependency") == source_id

    def test_expand_with_chain_context(self):
        """Test expansion with chained context inheritance."""
        builder = OperationGraphBuilder()
        source_id = builder.add_operation(operation="operate", instruction="Source")

        items = ["item1", "item2", "item3"]

        new_ids = builder.expand_from_result(
            items=items,
            source_node_id=source_id,
            operation="chat",
            strategy=ExpansionStrategy.SEQUENTIAL,
            inherit_context=True,
            chain_context=True,
        )

        # First node inherits from source
        node1 = builder._operations[new_ids[0]]
        assert node1.metadata.get("primary_dependency") == source_id

        # Second and third inherit from previous
        node2 = builder._operations[new_ids[1]]
        assert node2.metadata.get("primary_dependency") == new_ids[0]

        node3 = builder._operations[new_ids[2]]
        assert node3.metadata.get("primary_dependency") == new_ids[1]

    def test_expand_invalid_source(self):
        """Test expansion with invalid source node."""
        builder = OperationGraphBuilder()

        with pytest.raises(ValueError, match="Source node .* not found"):
            builder.expand_from_result(
                items=["item"],
                source_node_id="invalid_id",
                operation="chat",
            )


class TestOperationGraphBuilderAggregation:
    """Test aggregation operations."""

    def test_add_aggregation_basic(self):
        """Test basic aggregation operation."""
        builder = OperationGraphBuilder()

        # Create multiple nodes to aggregate from
        id1 = builder.add_operation(operation="chat", instruction="First")
        id2 = builder.add_operation(operation="chat", instruction="Second")
        id3 = builder.add_operation(operation="chat", instruction="Third")

        # Add aggregation
        agg_id = builder.add_aggregation(
            operation="operate",
            source_node_ids=[id1, id2, id3],
            instruction="Aggregate results",
        )

        assert agg_id in builder._operations
        node = builder._operations[agg_id]
        assert node.operation == "operate"
        assert node.parameters["aggregation_count"] == 3
        assert len(node.parameters["aggregation_sources"]) == 3
        assert node.metadata.get("aggregation") is True

        # Verify edges
        edges = [
            e for e in builder.graph.internal_edges if e.tail == agg_id and "aggregate" in e.label
        ]
        assert len(edges) == 3

    def test_add_aggregation_from_current_heads(self):
        """Test aggregation using current heads."""
        builder = OperationGraphBuilder()

        # Add operations
        builder.add_operation(operation="chat", instruction="First")

        # Expand creates multiple heads
        items = ["a", "b", "c"]
        builder.expand_from_result(
            items=items,
            source_node_id=builder.last_operation_id,
            operation="chat",
        )

        # Aggregate from current heads
        agg_id = builder.add_aggregation(operation="operate", instruction="Combine all")

        node = builder._operations[agg_id]
        assert node.parameters["aggregation_count"] == 3

    def test_add_aggregation_no_sources(self):
        """Test aggregation with no sources raises error."""
        builder = OperationGraphBuilder()

        with pytest.raises(ValueError, match="No source nodes"):
            builder.add_aggregation(
                operation="operate",
                source_node_ids=[],
                instruction="Invalid",
            )

    def test_add_aggregation_with_inherit_context(self):
        """Test aggregation with context inheritance."""
        builder = OperationGraphBuilder()

        id1 = builder.add_operation(operation="chat", instruction="First")
        id2 = builder.add_operation(operation="chat", instruction="Second")

        agg_id = builder.add_aggregation(
            operation="operate",
            source_node_ids=[id1, id2],
            inherit_context=True,
            inherit_from_source=1,
            instruction="Aggregate",
        )

        node = builder._operations[agg_id]
        assert node.metadata.get("inherit_context") is True
        assert node.metadata.get("primary_dependency") == id2
        assert node.metadata.get("inherit_from_source") == 1

    def test_add_aggregation_with_reference_id(self):
        """Test aggregation with reference ID."""
        builder = OperationGraphBuilder()

        id1 = builder.add_operation(operation="chat", instruction="First")

        agg_id = builder.add_aggregation(
            operation="operate",
            node_id="agg_ref",
            source_node_ids=[id1],
            instruction="Aggregate",
        )

        retrieved = builder.get_node_by_reference("agg_ref")
        assert retrieved is not None
        assert retrieved.id == agg_id


class TestOperationGraphBuilderConditionalBranching:
    """Test conditional branching."""

    def test_add_conditional_branch_with_both_branches(self):
        """Test adding conditional branch with true and false paths."""
        builder = OperationGraphBuilder()

        # Add initial operation
        builder.add_operation(operation="chat", instruction="Start")

        # Add conditional branch
        result = builder.add_conditional_branch(
            condition_check_op="operate",
            true_op="chat",
            false_op="chat",
            condition="test_condition",
        )

        assert "check" in result
        assert "true" in result
        assert "false" in result

        # Verify nodes were created
        check_node = builder._operations[result["check"]]
        assert check_node.parameters["is_condition_check"] is True
        assert check_node.parameters["condition"] == "test_condition"

        true_node = builder._operations[result["true"]]
        assert true_node.parameters["branch"] == "true"

        false_node = builder._operations[result["false"]]
        assert false_node.parameters["branch"] == "false"

        # Verify edges
        edges = list(builder.graph.internal_edges)
        # Should have: start->check, check->true, check->false
        assert len(edges) >= 3

        # Current heads should be both branches
        assert set(builder._current_heads) == {
            result["true"],
            result["false"],
        }

    def test_add_conditional_branch_true_only(self):
        """Test conditional branch with only true path."""
        builder = OperationGraphBuilder()

        builder.add_operation(operation="chat", instruction="Start")

        result = builder.add_conditional_branch(
            condition_check_op="operate",
            true_op="chat",
            false_op=None,
        )

        assert "check" in result
        assert "true" in result
        assert "false" not in result

        # Current heads should only have true branch
        assert builder._current_heads == [result["true"]]

    def test_conditional_branch_edges(self):
        """Test conditional branch edge labels."""
        builder = OperationGraphBuilder()

        builder.add_operation(operation="chat", instruction="Start")

        result = builder.add_conditional_branch(
            condition_check_op="operate",
            true_op="chat",
            false_op="chat",
        )

        # Find edges with specific labels
        edges = list(builder.graph.internal_edges)

        to_condition = [e for e in edges if "to_condition" in e.label]
        assert len(to_condition) == 1

        if_true = [e for e in edges if "if_true" in e.label]
        assert len(if_true) == 1
        assert if_true[0].head == result["check"]
        assert if_true[0].tail == result["true"]

        if_false = [e for e in edges if "if_false" in e.label]
        assert len(if_false) == 1
        assert if_false[0].head == result["check"]
        assert if_false[0].tail == result["false"]


class TestOperationGraphBuilderState:
    """Test builder state management."""

    def test_mark_executed(self):
        """Test marking operations as executed."""
        builder = OperationGraphBuilder()

        id1 = builder.add_operation(operation="chat", instruction="First")
        id2 = builder.add_operation(operation="chat", instruction="Second")

        assert len(builder._executed) == 0

        builder.mark_executed([id1])
        assert id1 in builder._executed
        assert id2 not in builder._executed

        builder.mark_executed([id2])
        assert len(builder._executed) == 2

    def test_get_unexecuted_nodes(self):
        """Test getting unexecuted nodes."""
        builder = OperationGraphBuilder()

        id1 = builder.add_operation(operation="chat", instruction="First")
        id2 = builder.add_operation(operation="chat", instruction="Second")
        id3 = builder.add_operation(operation="chat", instruction="Third")

        # Initially all unexecuted
        unexecuted = builder.get_unexecuted_nodes()
        assert len(unexecuted) == 3

        # Mark some executed
        builder.mark_executed([id1, id2])
        unexecuted = builder.get_unexecuted_nodes()
        assert len(unexecuted) == 1
        assert unexecuted[0].id == id3

    def test_get_graph(self):
        """Test getting the graph."""
        builder = OperationGraphBuilder()

        builder.add_operation(operation="chat", instruction="Test")

        graph = builder.get_graph()
        assert graph == builder.graph
        assert len(list(graph.internal_nodes)) == 1

    def test_get_node_by_reference_not_found(self):
        """Test get_node_by_reference returns None when not found."""
        builder = OperationGraphBuilder()

        builder.add_operation(operation="chat", node_id="ref1", instruction="Test")

        assert builder.get_node_by_reference("nonexistent") is None


class TestOperationGraphBuilderVisualization:
    """Test visualization and state inspection."""

    def test_visualize_state(self):
        """Test visualize_state returns correct statistics."""
        builder = OperationGraphBuilder(name="TestGraph")

        id1 = builder.add_operation(operation="chat", instruction="First")
        id2 = builder.add_operation(operation="chat", instruction="Second")

        # Expand to create expansion tracking
        items = ["a", "b"]
        builder.expand_from_result(
            items=items,
            source_node_id=id2,
            operation="chat",
        )

        state = builder.visualize_state()

        assert state["name"] == "TestGraph"
        assert state["total_nodes"] == 4  # 2 original + 2 expanded
        assert state["executed_nodes"] == 0
        assert state["unexecuted_nodes"] == 4
        assert len(state["current_heads"]) == 2
        assert state["edges"] >= 3

        # Test with executed nodes
        builder.mark_executed([id1])
        state = builder.visualize_state()
        assert state["executed_nodes"] == 1
        assert state["unexecuted_nodes"] == 3

    def test_visualize_state_expansions(self):
        """Test visualize_state tracks expansions correctly."""
        builder = OperationGraphBuilder()

        source_id = builder.add_operation(operation="operate", instruction="Source")

        items = ["a", "b", "c"]
        new_ids = builder.expand_from_result(
            items=items,
            source_node_id=source_id,
            operation="chat",
        )

        state = builder.visualize_state()
        assert source_id in state["expansions"]
        assert len(state["expansions"][source_id]) == 3


class TestOperationGraphBuilderComplexScenarios:
    """Test complex multi-stage scenarios."""

    def test_multi_stage_expansion(self):
        """Test multiple expansion stages."""
        builder = OperationGraphBuilder()

        # Stage 1: Initial operation
        stage1_id = builder.add_operation(
            operation="operate", instruction="Generate ideas", num_ideas=3
        )

        # Stage 2: Expand into parallel processing
        items_stage2 = ["idea1", "idea2", "idea3"]
        stage2_ids = builder.expand_from_result(
            items=items_stage2,
            source_node_id=stage1_id,
            operation="chat",
            strategy=ExpansionStrategy.CONCURRENT,
        )

        # Stage 3: Aggregate results
        stage3_id = builder.add_aggregation(
            operation="operate",
            source_node_ids=stage2_ids,
            instruction="Synthesize results",
        )

        # Verify structure
        assert len(builder._operations) == 5  # 1 + 3 + 1
        assert builder.last_operation_id == stage3_id

        state = builder.visualize_state()
        assert state["total_nodes"] == 5

    def test_complex_conditional_flow(self):
        """Test complex flow with conditionals and aggregation."""
        builder = OperationGraphBuilder()

        # Initial analysis
        start_id = builder.add_operation(operation="operate", instruction="Analyze input")

        # Conditional branch
        cond_result = builder.add_conditional_branch(
            condition_check_op="operate",
            true_op="chat",
            false_op="chat",
        )

        # Aggregate both branches
        agg_id = builder.add_aggregation(
            operation="operate",
            source_node_ids=[cond_result["true"], cond_result["false"]],
            instruction="Combine results",
        )

        # Verify structure
        assert len(builder._operations) == 5
        assert builder.last_operation_id == agg_id

    def test_sequential_concurrent_mixed(self):
        """Test mixing sequential and concurrent operations."""
        builder = OperationGraphBuilder()

        # Sequential stage 1
        seq1_id = builder.add_operation(operation="chat", instruction="Step 1")

        # Concurrent stage 2
        items = ["a", "b", "c"]
        concurrent_ids = builder.expand_from_result(
            items=items,
            source_node_id=seq1_id,
            operation="chat",
            strategy=ExpansionStrategy.CONCURRENT,
        )

        # Sequential stage 3 (aggregate)
        seq2_id = builder.add_aggregation(
            operation="operate",
            source_node_ids=concurrent_ids,
            instruction="Final step",
        )

        # Verify final structure
        assert len(builder._operations) == 5
        assert builder.last_operation_id == seq2_id
        assert builder._current_heads == [seq2_id]
