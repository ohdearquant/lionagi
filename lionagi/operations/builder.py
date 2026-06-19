# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Incremental graph builder for multi-stage operations."""

from enum import Enum
from typing import Any

from lionagi._errors import OperationError
from lionagi.models.note import Note
from lionagi.operations.node import create_operation
from lionagi.protocols.graph.edge import Edge
from lionagi.protocols.types import ID

__all__ = (
    "OperationGraphBuilder",
    "ExpansionStrategy",
)


class ExpansionStrategy(Enum):
    CONCURRENT = "concurrent"
    SEQUENTIAL = "sequential"
    SEQUENTIAL_CONCURRENT_CHUNK = "sequential_concurrent_chunk"
    CONCURRENT_SEQUENTIAL_CHUNK = "concurrent_sequential_chunk"


class OperationGraphBuilder:
    """Incremental graph builder supporting build/execute/expand cycles."""

    def __init__(self, name: str = "DynamicGraph"):
        from lionagi.protocols.graph.graph import Graph

        self.name = name
        self.graph = Graph()

        self._operations = {}
        self._executed: set[str] = set()
        self._current_heads: list[str] = []
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
        node = create_operation(operation=operation, parameters=parameters)

        if inherit_context and depends_on:
            node.metadata["inherit_context"] = True
            node.metadata["primary_dependency"] = depends_on[0]

        self.graph.add_node(node)
        self._operations[node.id] = node

        if node_id:
            node.metadata["reference_id"] = node_id

        if branch:
            node.branch_id = ID.get_id(branch)

        if depends_on:
            for dep_id in depends_on:
                if dep_id in self._operations:
                    edge = Edge(head=dep_id, tail=node.id, label=["depends_on"])
                    self.graph.add_edge(edge)
        elif self._current_heads:
            for head_id in self._current_heads:
                edge = Edge(head=head_id, tail=node.id, label=["sequential"])
                self.graph.add_edge(edge)

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
        if source_node_id not in self._operations:
            raise OperationError(f"Source node {source_node_id} not found")

        new_node_ids = []

        for i, item in enumerate(items):
            if hasattr(item, "model_dump"):
                params = {**item.model_dump(), **shared_params}
            else:
                params = {**shared_params, "item_index": i, "item": str(item)}

            params["expanded_from"] = source_node_id
            params["expansion_strategy"] = strategy.value

            node_meta = Note(
                expansion_index=i,
                expansion_source=source_node_id,
                expansion_strategy=strategy.value,
            )
            if inherit_context:
                node_meta["inherit_context"] = True
                if chain_context and strategy == ExpansionStrategy.SEQUENTIAL and i > 0:
                    node_meta["primary_dependency"] = new_node_ids[i - 1]
                else:
                    node_meta["primary_dependency"] = source_node_id

            node = create_operation(
                operation=operation,
                parameters=params,
                metadata=node_meta.content,
            )

            self.graph.add_node(node)
            self._operations[node.id] = node
            new_node_ids.append(node.id)

            edge = Edge(
                head=source_node_id,
                tail=node.id,
                label=["expansion", strategy.value],
            )
            self.graph.add_edge(edge)

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
        sources = source_node_ids or self._current_heads
        if not sources:
            raise OperationError("No source nodes for aggregation")

        agg_params = {
            "aggregation_sources": [str(s) for s in sources],
            "aggregation_count": len(sources),
            **parameters,
        }

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

        for source_id in sources:
            edge = Edge(head=source_id, tail=node.id, label=["aggregate"])
            self.graph.add_edge(edge)

        self._current_heads = [node.id]
        self.last_operation_id = node.id

        return node.id

    def mark_executed(self, node_ids: list[str]):
        self._executed.update(node_ids)

    def get_unexecuted_nodes(self):
        return [op for op_id, op in self._operations.items() if op_id not in self._executed]

    def add_conditional_branch(
        self,
        condition_check_op: str,
        true_op: str,
        false_op: str | None = None,
        **check_params,
    ) -> dict[str, str]:
        check_node = create_operation(
            operation=condition_check_op,
            parameters={**check_params, "is_condition_check": True},
        )
        self.graph.add_node(check_node)
        self._operations[check_node.id] = check_node

        for head_id in self._current_heads:
            edge = Edge(head=head_id, tail=check_node.id, label=["to_condition"])
            self.graph.add_edge(edge)

        result = {"check": check_node.id}

        true_node = create_operation(operation=true_op, parameters={"branch": "true"})
        self.graph.add_node(true_node)
        self._operations[true_node.id] = true_node
        result["true"] = true_node.id

        true_edge = Edge(head=check_node.id, tail=true_node.id, label=["if_true"])
        self.graph.add_edge(true_edge)

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
        return self.graph

    def get_node_by_reference(self, reference_id: str):
        for op in self._operations.values():
            if op.metadata.get("reference_id") == reference_id:
                return op
        return None

    def visualize_state(self) -> dict[str, Any]:
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
