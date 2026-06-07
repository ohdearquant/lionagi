# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
OperationGraphBuilder: Incremental graph builder for multi-stage operations.

Build → Execute → Expand → Execute → ...
"""

from enum import Enum
from typing import Any

from lionagi.models.note import Note
from lionagi.operations.node import create_operation
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.types import ID

__all__ = (
    "OperationGraphBuilder",
    "ExpansionStrategy",
)


class ExpansionStrategy(Enum):
    """Strategies for expanding operations."""

    CONCURRENT = "concurrent"
    SEQUENTIAL = "sequential"
    SEQUENTIAL_CONCURRENT_CHUNK = "sequential_concurrent_chunk"
    CONCURRENT_SEQUENTIAL_CHUNK = "concurrent_sequential_chunk"


class OperationGraphBuilder:
    """
    Incremental graph builder that supports build → execute → expand cycles.

    Unlike static builders, this maintains state and allows expanding the graph
    based on execution results.

    Examples:
        >>> # Build initial graph
        >>> builder = OperationGraphBuilder()
        >>> builder.add_operation("operate", instruction="Generate ideas", num_instruct=5)
        >>> graph = builder.get_graph()
        >>>
        >>> # Execute with session
        >>> result = await session.flow(graph)
        >>>
        >>> # Expand based on results
        >>> if hasattr(result, 'instruct_model'):
        ...     builder.expand_from_result(
        ...         result.instruct_model,
        ...         source_node_id=builder.last_operation_id,
        ...         operation="operate"
        ...     )
        >>>
        >>> # Get expanded graph and continue execution
        >>> graph = builder.get_graph()
        >>> final_result = await session.flow(graph)
    """

    def __init__(self, name: str = "DynamicGraph"):
        """Initialize the incremental graph builder."""
        from lionagi.protocols.graph.graph import Graph

        self.name = name
        self.graph = Graph()

        # Track state
        self._operations = {}  # All operations by ID
        self._executed: set[str] = set()  # IDs of executed operations
        self._current_heads: list[str] = []  # Current head nodes for linking
        self.last_operation_id: str | None = None

    def add_operation(
        self,
        operation: str,
        node_id: str | None = None,
        depends_on: list[str] | None = None,
        inherit_context: bool = False,
        branch=None,
        **parameters,
    ) -> str:
        """
        Add an operation to the graph.

        Args:
            operation: The branch operation
            node_id: Optional ID reference for this node
            depends_on: List of node IDs this depends on
            inherit_context: If True and has dependencies, inherit conversation
                           context from primary (first) dependency
            **parameters: Operation parameters

        Returns:
            ID of the created node
        """
        # Create operation node
        node = create_operation(operation=operation, parameters=parameters)

        # Store context inheritance strategy
        if inherit_context and depends_on:
            node.metadata["inherit_context"] = True
            node.metadata["primary_dependency"] = depends_on[0]

        self.graph.add_node(node)
        self._operations[node.id] = node

        # Store reference if provided
        if node_id:
            # Add as metadata for easy lookup
            node.metadata["reference_id"] = node_id

        if branch:
            node.branch_id = ID.get_id(branch)

        # Handle dependencies
        if depends_on:
            for dep_id in depends_on:
                if dep_id in self._operations:
                    edge = Edge(head=dep_id, tail=node.id, label=["depends_on"])
                    self.graph.add_edge(edge)
        elif self._current_heads:
            # Auto-link from current heads
            for head_id in self._current_heads:
                edge = Edge(head=head_id, tail=node.id, label=["sequential"])
                self.graph.add_edge(edge)

        # Update state
        self._current_heads = [node.id]
        self.last_operation_id = node.id

        return node.id

    def expand_from_result(
        self,
        items: list[Any],
        source_node_id: str,
        operation: str,
        strategy: ExpansionStrategy = ExpansionStrategy.CONCURRENT,
        inherit_context: bool = False,
        chain_context: bool = False,
        **shared_params,
    ) -> list[str]:
        """
        Expand the graph based on execution results.

        This is called after executing the graph to add new operations
        based on results.

        Args:
            items: Items from result to expand (e.g., instruct_model)
            source_node_id: ID of node that produced these items
            operation: Operation to apply to each item
            strategy: How to organize the expanded operations
            inherit_context: If True, expanded operations inherit context from source
            chain_context: If True and strategy is SEQUENTIAL, each operation
                          inherits from the previous (only applies to SEQUENTIAL)
            **shared_params: Shared parameters for all operations

        Returns:
            List of new node IDs
        """
        if source_node_id not in self._operations:
            raise ValueError(f"Source node {source_node_id} not found")

        new_node_ids = []

        # Create operation for each item
        for i, item in enumerate(items):
            # Extract parameters from item if it's a model
            if hasattr(item, "model_dump"):
                params = {**item.model_dump(), **shared_params}
            else:
                params = {**shared_params, "item_index": i, "item": str(item)}

            # Add metadata about expansion
            params["expanded_from"] = source_node_id
            params["expansion_strategy"] = strategy.value

            # Build node metadata in one place before creation
            node_meta = Note(
                expansion_index=i,
                expansion_source=source_node_id,
                expansion_strategy=strategy.value,
            )
            if inherit_context:
                node_meta["inherit_context"] = True
                if chain_context and strategy == ExpansionStrategy.SEQUENTIAL and i > 0:
                    # Chain context: inherit from previous expanded operation
                    node_meta["primary_dependency"] = new_node_ids[i - 1]
                else:
                    # Inherit from source node
                    node_meta["primary_dependency"] = source_node_id

            node = create_operation(
                operation=operation,
                parameters=params,
                metadata=node_meta.content,
            )

            self.graph.add_node(node)
            self._operations[node.id] = node
            new_node_ids.append(node.id)

            # Link from source
            edge = Edge(
                head=source_node_id,
                tail=node.id,
                label=["expansion", strategy.value],
            )
            self.graph.add_edge(edge)

        # Update current heads based on strategy
        if strategy in [
            ExpansionStrategy.CONCURRENT,
            ExpansionStrategy.SEQUENTIAL,
        ]:
            self._current_heads = new_node_ids

        return new_node_ids

    def add_aggregation(
        self,
        operation: str,
        node_id: str | None = None,
        source_node_ids: list[str] | None = None,
        inherit_context: bool = False,
        inherit_from_source: int = 0,
        branch=None,
        **parameters,
    ) -> str:
        """
        Add an aggregation operation that collects from multiple sources.

        Args:
            operation: Aggregation operation
            node_id: Optional ID reference for this node
            source_node_ids: Nodes to aggregate from (defaults to current heads)
            inherit_context: If True, inherit conversation context from one source
            inherit_from_source: Index of source to inherit context from (default: 0)
            **parameters: Operation parameters

        Returns:
            ID of aggregation node
        """
        sources = source_node_ids or self._current_heads
        if not sources:
            raise ValueError("No source nodes for aggregation")

        # Add aggregation metadata - convert UUID to strings for JSON serialization
        agg_params = {
            "aggregation_sources": [str(s) for s in sources],
            "aggregation_count": len(sources),
            **parameters,
        }

        # Build aggregation metadata in one place before creation
        agg_meta = Note(aggregation=True)
        if node_id:
            agg_meta["reference_id"] = node_id
        if inherit_context and sources:
            source_idx = min(inherit_from_source, len(sources) - 1)
            agg_meta["inherit_context"] = True
            agg_meta["primary_dependency"] = sources[source_idx]
            agg_meta["inherit_from_source"] = source_idx

        node = create_operation(
            operation=operation,
            parameters=agg_params,
            metadata=agg_meta.content,
        )

        if branch:
            node.branch_id = ID.get_id(branch)

        self.graph.add_node(node)
        self._operations[node.id] = node

        # Connect all sources
        for source_id in sources:
            edge = Edge(head=source_id, tail=node.id, label=["aggregate"])
            self.graph.add_edge(edge)

        # Update state
        self._current_heads = [node.id]
        self.last_operation_id = node.id

        return node.id

    def mark_executed(self, node_ids: list[str]):
        """
        Mark nodes as executed.

        This helps track which parts of the graph have been run.

        Args:
            node_ids: IDs of executed nodes
        """
        self._executed.update(node_ids)

    def get_unexecuted_nodes(self):
        """
        Get nodes that haven't been executed yet.

        Returns:
            List of unexecuted operations
        """
        return [op for op_id, op in self._operations.items() if op_id not in self._executed]

    def add_conditional_branch(
        self,
        condition_check_op: str,
        true_op: str,
        false_op: str | None = None,
        **check_params,
    ) -> dict[str, str]:
        """
        Add a conditional branch structure.

        Args:
            condition_check_op: Operation that evaluates condition
            true_op: Operation if condition is true
            false_op: Operation if condition is false
            **check_params: Parameters for condition check

        Returns:
            Dict with node IDs: {'check': id, 'true': id, 'false': id}
        """
        # Add condition check node
        check_node = create_operation(
            operation=condition_check_op,
            parameters={**check_params, "is_condition_check": True},
        )
        self.graph.add_node(check_node)
        self._operations[check_node.id] = check_node

        # Link from current heads
        for head_id in self._current_heads:
            edge = Edge(head=head_id, tail=check_node.id, label=["to_condition"])
            self.graph.add_edge(edge)

        result = {"check": check_node.id}

        # Add true branch
        true_node = create_operation(operation=true_op, parameters={"branch": "true"})
        self.graph.add_node(true_node)
        self._operations[true_node.id] = true_node
        result["true"] = true_node.id

        # Connect with condition label
        true_edge = Edge(head=check_node.id, tail=true_node.id, label=["if_true"])
        self.graph.add_edge(true_edge)

        # Add false branch if specified
        if false_op:
            false_node = create_operation(operation=false_op, parameters={"branch": "false"})
            self.graph.add_node(false_node)
            self._operations[false_node.id] = false_node
            result["false"] = false_node.id

            false_edge = Edge(head=check_node.id, tail=false_node.id, label=["if_false"])
            self.graph.add_edge(false_edge)

            self._current_heads = [true_node.id, false_node.id]
        else:
            self._current_heads = [true_node.id]

        return result

    def get_graph(self):
        """
        Get the current graph for execution.

        Returns:
            The graph in its current state
        """
        return self.graph

    def get_node_by_reference(self, reference_id: str):
        """
        Get a node by its reference ID.

        Args:
            reference_id: The reference ID assigned when creating the node

        Returns:
            The operation node or None
        """
        for op in self._operations.values():
            if op.metadata.get("reference_id") == reference_id:
                return op
        return None

    def visualize_state(self) -> dict[str, Any]:
        """
        Get visualization of current graph state.

        Returns:
            Dict with graph statistics and state
        """
        # Group nodes by expansion source
        expansions = {}
        for op in self._operations.values():
            source = op.metadata.get("expansion_source")
            if source:
                if source not in expansions:
                    expansions[source] = []
                expansions[source].append(op.id)

        return {
            "name": self.name,
            "total_nodes": len(self._operations),
            "executed_nodes": len(self._executed),
            "unexecuted_nodes": len(self._operations) - len(self._executed),
            "current_heads": self._current_heads,
            "expansions": expansions,
            "edges": len(self.graph.internal_edges),
        }

    def visualize(self, title: str = "Operation Graph", figsize=(14, 10)):
        from ._visualize_graph import visualize_graph

        visualize_graph(
            self,
            title=title,
            figsize=figsize,
        )
