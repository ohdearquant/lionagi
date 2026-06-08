# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import threading
from collections import deque
from typing import Any, Generic, Literal, TypeVar

from pydantic import (
    Field,
    PrivateAttr,
    field_serializer,
    field_validator,
    model_validator,
)
from typing_extensions import Self

from lionagi._errors import ItemExistsError, RelationError
from lionagi.ln._utils import synchronized as _graph_synchronized

from .._concepts import Relational
from ..generic.element import ID, Element
from ..generic.pile import Pile
from .edge import Edge
from .node import Node

T = TypeVar("T", bound=Node)

_NETWORKX_AVAILABLE = None
_MATPLIB_AVAILABLE = None
__all__ = ("Graph",)


class Graph(Element, Relational, Generic[T]):
    """Directed graph of Nodes and Edges with adjacency mapping."""

    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)

    internal_nodes: Pile[T] = Field(
        default_factory=lambda: Pile(item_type={Node}, strict_type=False),
        title="Internal Nodes",
        description="A collection of nodes in the graph.",
    )
    internal_edges: Pile[Edge] = Field(
        default_factory=lambda: Pile(item_type={Edge}, strict_type=False),
        title="Internal Edges",
        description="A collection of edges in the graph.",
    )
    node_edge_mapping: dict = Field(default_factory=dict, exclude=True)

    @model_validator(mode="after")
    def _validate_node_mapping(self) -> Self:
        self.node_edge_mapping = {}
        if self.internal_nodes:
            for node in self.internal_nodes:
                if node.id not in self.node_edge_mapping:
                    self.node_edge_mapping[node.id] = {"in": {}, "out": {}}

        if self.internal_edges:
            for edge in self.internal_edges:
                self.node_edge_mapping[edge.head]["out"][edge.id] = edge.tail
                self.node_edge_mapping[edge.tail]["in"][edge.id] = edge.head
        return self

    @field_serializer("internal_nodes", "internal_edges")
    def _serialize_nodes_edges(self, value: Pile):
        return value.to_dict()

    @field_validator("internal_nodes", "internal_edges", mode="before")
    def _deserialize_nodes_edges(cls, value: Any):
        return Pile.from_dict(value)

    @_graph_synchronized
    def add_node(self, node: Relational) -> None:
        if not isinstance(node, Relational):
            raise RelationError("Failed to add node: Invalid node type: not a <Relational> entity.")
        _id = ID.get_id(node)
        try:
            self.internal_nodes.insert(len(self.internal_nodes), node)
            self.node_edge_mapping[_id] = {"in": {}, "out": {}}
        except ItemExistsError as e:
            raise RelationError(f"Error adding node: {e}") from e

    @_graph_synchronized
    def add_edge(self, edge: Edge, /) -> None:
        if not isinstance(edge, Edge):
            raise RelationError("Failed to add edge: Invalid edge type.")
        if edge.head not in self.internal_nodes or edge.tail not in self.internal_nodes:
            raise RelationError(
                "Failed to add edge: Either edge head or tail node does not exist in the graph."
            )
        try:
            self.internal_edges.insert(len(self.internal_edges), edge)
            self.node_edge_mapping[edge.head]["out"][edge.id] = edge.tail
            self.node_edge_mapping[edge.tail]["in"][edge.id] = edge.head
        except ItemExistsError as e:
            raise RelationError(f"Error adding node: {e}") from e

    @_graph_synchronized
    def remove_node(self, node: ID[Node].Ref, /) -> None:
        _id = ID.get_id(node)
        if _id not in self.internal_nodes:
            raise RelationError(f"Node {node} not found in the graph nodes.")

        in_edges: dict = self.node_edge_mapping[_id]["in"]
        for edge_id, node_id in in_edges.items():
            self.node_edge_mapping[node_id]["out"].pop(edge_id)
            self.internal_edges.pop(edge_id)

        out_edges: dict = self.node_edge_mapping[_id]["out"]
        for edge_id, node_id in out_edges.items():
            self.node_edge_mapping[node_id]["in"].pop(edge_id)
            self.internal_edges.pop(edge_id)

        self.node_edge_mapping.pop(_id)
        return self.internal_nodes.pop(_id)

    @_graph_synchronized
    def remove_edge(self, edge: Edge | str, /) -> None:
        _id = ID.get_id(edge)
        if _id not in self.internal_edges:
            raise RelationError(f"Edge {edge} not found in the graph edges.")

        edge = self.internal_edges[_id]
        self.node_edge_mapping[edge.head]["out"].pop(_id)
        self.node_edge_mapping[edge.tail]["in"].pop(_id)

        return self.internal_edges.pop(_id)

    def find_node_edge(
        self,
        node: Any,
        /,
        direction: Literal["both", "in", "out"] = "both",
    ) -> list[Edge]:
        if direction not in {"both", "in", "out"}:
            raise ValueError("The direction should be 'both', 'in', or 'out'.")

        _id = ID.get_id(node)
        if _id not in self.internal_nodes:
            raise RelationError(f"Node {node} not found in the graph nodes.")

        result = []
        if direction in {"both", "in"}:
            for edge_id in self.node_edge_mapping[_id]["in"]:
                result.append(self.internal_edges[edge_id])

        if direction in {"both", "out"}:
            for edge_id in self.node_edge_mapping[_id]["out"]:
                result.append(self.internal_edges[edge_id])

        return Pile(result, item_type={Edge}, strict_type=False)

    def get_heads(self) -> Pile[Node]:
        result = []
        for node_id in self.node_edge_mapping.keys():
            if self.node_edge_mapping[node_id]["in"] == {}:
                result.append(self.internal_nodes[node_id])
        return Pile(result, item_type={Node}, strict_type=False)

    def get_predecessors(self, node: Node, /) -> Pile[Node]:
        edges = self.find_node_edge(node, direction="in")
        result = []
        for edge in edges:
            result.append(self.internal_nodes[edge.head])
        return Pile(result, item_type={Node}, strict_type=False)

    def get_successors(self, node: Node, /) -> Pile[Node]:
        edges = self.find_node_edge(node, direction="out")
        result = []
        for edge in edges:
            result.append(self.internal_nodes[edge.tail])
        return Pile(result, item_type={Node}, strict_type=False)

    def to_networkx(self, **kwargs) -> Any:
        global _NETWORKX_AVAILABLE
        if _NETWORKX_AVAILABLE is None:
            from lionagi.ln import is_import_installed

            _NETWORKX_AVAILABLE = is_import_installed("networkx")

        if _NETWORKX_AVAILABLE is not True:
            raise ImportError(
                "The 'networkx' package is required for this feature. "
                "Please install `networkx` or `'lionagi[graph]'`."
            )

        from networkx import DiGraph  # type: ignore

        g = DiGraph(**kwargs)
        for node in self.internal_nodes:
            node_info = node.to_dict()
            node_info.pop("id")
            g.add_node(str(node.id), **node_info)

        for _edge in self.internal_edges:
            edge_info = _edge.to_dict()
            edge_info.pop("id")
            source_node_id = edge_info.pop("head")
            target_node_id = edge_info.pop("tail")
            g.add_edge(str(source_node_id), str(target_node_id), **edge_info)

        return g

    def display(
        self,
        node_label="lion_class",
        edge_label="label",
        draw_kwargs=None,
        **kwargs,
    ):
        if draw_kwargs is None:
            draw_kwargs = {}
        g = self.to_networkx(**kwargs)

        global _MATPLIB_AVAILABLE
        if _MATPLIB_AVAILABLE is None:
            from lionagi.ln import is_import_installed

            _MATPLIB_AVAILABLE = is_import_installed("matplotlib")

        if _MATPLIB_AVAILABLE is not True:
            raise ImportError(
                "The 'matplotlib' package is required for this feature. "
                "Please install `matplotlib` or `'lionagi[graph]'`."
            )

        import matplotlib.pyplot as plt  # type: ignore
        import networkx as nx  # type: ignore

        pos = nx.spring_layout(g)
        nx.draw(
            g,
            pos,
            labels=nx.get_node_attributes(g, node_label),
            **draw_kwargs,
        )

        edge_labels = nx.get_edge_attributes(g, edge_label)
        if edge_labels:
            nx.draw_networkx_edge_labels(g, pos, edge_labels=edge_labels)

        plt.axis("off")
        plt.show()

    def is_acyclic(self) -> bool:
        node_ids = list(self.internal_nodes.progression)
        check_deque = deque(node_ids)

        check_dict = {nid: 0 for nid in node_ids}

        def visit(nid):
            if check_dict[nid] == 2:
                return True
            elif check_dict[nid] == 1:
                return False

            check_dict[nid] = 1
            for edge_id in self.node_edge_mapping[nid]["out"]:
                edge: Edge = self.internal_edges[edge_id]
                if not visit(edge.tail):
                    return False
            check_dict[nid] = 2
            return True

        while check_deque:
            key = check_deque.pop()
            if not visit(key):
                return False
        return True

    def get_tails(self) -> Pile[Node]:
        result = []
        for node_id in self.node_edge_mapping:
            if self.node_edge_mapping[node_id]["out"] == {}:
                result.append(self.internal_nodes[node_id])
        return Pile(result, item_type={Node}, strict_type=False)

    def topological_sort(self) -> list[Node]:
        in_degree = {}
        for node_id in self.node_edge_mapping:
            in_degree[node_id] = len(self.node_edge_mapping[node_id]["in"])

        queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
        result: list[Node] = []

        while queue:
            node_id = queue.popleft()
            result.append(self.internal_nodes[node_id])

            for edge_id in self.node_edge_mapping[node_id]["out"]:
                edge: Edge = self.internal_edges[edge_id]
                neighbor_id = edge.tail
                in_degree[neighbor_id] -= 1
                if in_degree[neighbor_id] == 0:
                    queue.append(neighbor_id)

        if len(result) != len(self.node_edge_mapping):
            raise ValueError("Cannot topologically sort graph with cycles")

        return result

    async def find_path(
        self,
        start: Any,
        end: Any,
        check_conditions: bool = False,
    ) -> list[Edge] | None:
        start_id = ID.get_id(start)
        end_id = ID.get_id(end)

        if start_id not in self.internal_nodes:
            raise RelationError(f"Start node {start_id} not found in graph")
        if end_id not in self.internal_nodes:
            raise RelationError(f"End node {end_id} not found in graph")

        if start_id == end_id:
            return []

        queue: deque = deque([start_id])
        parent: dict = {}  # node_id -> (parent_node_id, edge_id)
        visited = {start_id}

        while queue:
            current_id = queue.popleft()

            for edge_id in self.node_edge_mapping[current_id]["out"]:
                edge: Edge = self.internal_edges[edge_id]
                neighbor_id = edge.tail

                if neighbor_id not in visited:
                    if check_conditions and not await edge.check_condition():
                        continue
                    visited.add(neighbor_id)
                    parent[neighbor_id] = (current_id, edge_id)
                    queue.append(neighbor_id)

                    if neighbor_id == end_id:
                        path = []
                        node_id = end_id
                        while node_id in parent:
                            parent_id, eid = parent[node_id]
                            path.append(self.internal_edges[eid])
                            node_id = parent_id
                        return list(reversed(path))

        return None

    @_graph_synchronized
    def replace_node(self, old: Any, new_node: "Node") -> "Node":
        old_id = ID.get_id(old)
        if old_id not in self.internal_nodes:
            raise RelationError(f"Node {old_id} not found in graph")

        new_id = new_node.id
        if new_id in self.internal_nodes:
            raise RelationError(f"Replacement node {new_id} already in graph")

        self.internal_nodes.insert(len(self.internal_nodes), new_node)
        self.node_edge_mapping[new_id] = {"in": {}, "out": {}}

        mapping = self.node_edge_mapping[old_id]

        for edge_id, head_id in list(mapping["in"].items()):
            edge = self.internal_edges[edge_id]
            edge.tail = new_id
            self.node_edge_mapping[new_id]["in"][edge_id] = head_id
            self.node_edge_mapping[head_id]["out"][edge_id] = new_id

        for edge_id, tail_id in list(mapping["out"].items()):
            edge = self.internal_edges[edge_id]
            edge.head = new_id
            self.node_edge_mapping[new_id]["out"][edge_id] = tail_id
            self.node_edge_mapping[tail_id]["in"][edge_id] = new_id

        self.node_edge_mapping.pop(old_id)
        return self.internal_nodes.pop(old_id)

    @_graph_synchronized
    def splice_after(self, anchor: Any, new_node: "Node") -> list[Edge]:
        anchor_id = ID.get_id(anchor)
        if anchor_id not in self.internal_nodes:
            raise RelationError(f"Node {anchor_id} not found in graph")

        new_id = new_node.id
        if new_id in self.internal_nodes:
            raise RelationError(f"Node {new_id} already in graph")

        self.internal_nodes.insert(len(self.internal_nodes), new_node)
        self.node_edge_mapping[new_id] = {"in": {}, "out": {}}

        out_edges = list(self.node_edge_mapping[anchor_id]["out"].items())

        new_edges: list[Edge] = []
        for edge_id, tail_id in out_edges:
            old_edge = self.internal_edges[edge_id]
            extra_properties = {
                k: v for k, v in old_edge.properties.items() if k not in {"condition", "label"}
            }

            replacement = Edge(
                head=new_id,
                tail=tail_id,
                condition=old_edge.condition,
                label=old_edge.label,
                **extra_properties,
            )
            self.internal_edges.insert(len(self.internal_edges), replacement)
            self.node_edge_mapping[new_id]["out"][replacement.id] = tail_id
            self.node_edge_mapping[tail_id]["in"][replacement.id] = new_id

            self.node_edge_mapping[tail_id]["in"].pop(edge_id)
            self.node_edge_mapping[anchor_id]["out"].pop(edge_id)
            self.internal_edges.pop(edge_id)

            new_edges.append(replacement)

        link = Edge(head=anchor_id, tail=new_id)
        self.internal_edges.insert(len(self.internal_edges), link)
        self.node_edge_mapping[anchor_id]["out"][link.id] = new_id
        self.node_edge_mapping[new_id]["in"][link.id] = anchor_id

        new_edges.insert(0, link)
        return new_edges

    def __contains__(self, item: object) -> bool:
        return item in self.internal_nodes or item in self.internal_edges


# File: lionagi/protocols/graph/graph.py
