# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import threading
from collections import deque
from functools import wraps
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

from .._concepts import Relational
from ..generic.element import ID, Element
from ..generic.pile import Pile
from .edge import Edge
from .node import Node

T = TypeVar("T", bound=Node)

_NETWORKX_AVAILABLE = None
_MATPLIB_AVAILABLE = None
__all__ = ("Graph",)


def _graph_synchronized(func):
    """Synchronize a Graph mutation method using its reentrant lock."""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return func(self, *args, **kwargs)

    return wrapper


class Graph(Element, Relational, Generic[T]):
    """Directed graph of Nodes and Edges with adjacency mapping.

    Thread Safety:
        Mutation methods (``add_node``, ``add_edge``, ``remove_node``,
        ``remove_edge``) are protected by ``@_graph_synchronized``
        using an ``RLock``.  Read-only traversal methods
        (``get_tails``, ``topological_sort``, ``find_path``) are **not**
        synchronized — if the graph may be mutated concurrently,
        callers should hold ``graph._lock`` or use external
        synchronization during reads.
    """

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
        """
        Serialize the internal nodes and edges to a dictionary format.
        This is used for serialization purposes.
        """
        return value.to_dict()

    @field_validator("internal_nodes", "internal_edges", mode="before")
    def _deserialize_nodes_edges(cls, value: Any):
        return Pile.from_dict(value)

    @_graph_synchronized
    def add_node(self, node: Relational) -> None:
        """Add a node to the graph."""
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
        """Add an edge to the graph, linking two existing nodes."""
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
        """
        Remove a node from the graph.

        This method removes a node and all connected edges from the graph.

        Args:
            node (Node | str): The node or node ID to remove.

        Raises:
            RelationError: If the node does not exist in the graph.
        """
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
        """
        Remove an edge from the graph.

        Args:
            edge (Edge | str): The edge or edge ID to remove.

        Raises:
            RelationError: If the edge does not exist in the graph.
        """
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
        """
        Find edges associated with a node by direction (in, out, or both).

        Args:
            node (ID[Node].Ref): The node or node ID.
            direction: 'in', 'out', or 'both' to filter the edges.

        Returns:
            list[Edge]: The matching edges.

        Raises:
            ValueError: If direction is invalid.
            RelationError: If node is not in the graph.
        """
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
        """Return nodes with no incoming edges (head nodes)."""
        result = []
        for node_id in self.node_edge_mapping.keys():
            if self.node_edge_mapping[node_id]["in"] == {}:
                result.append(self.internal_nodes[node_id])
        return Pile(result, item_type={Node}, strict_type=False)

    def get_predecessors(self, node: Node, /) -> Pile[Node]:
        """Return all nodes that have outbound edges to the given node."""
        edges = self.find_node_edge(node, direction="in")
        result = []
        for edge in edges:
            result.append(self.internal_nodes[edge.head])
        return Pile(result, item_type={Node}, strict_type=False)

    def get_successors(self, node: Node, /) -> Pile[Node]:
        """
        Get all successor nodes of a given node.

        Successors are nodes that have incoming edges from the given node.

        Args:
            node (Node): The node to find successors for.

        Returns:
            Pile: A Pile containing all successor nodes.
        """
        edges = self.find_node_edge(node, direction="out")
        result = []
        for edge in edges:
            result.append(self.internal_nodes[edge.tail])
        return Pile(result, item_type={Node}, strict_type=False)

    def to_networkx(self, **kwargs) -> Any:
        """Convert the graph to a NetworkX graph object."""
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
        """Display the graph using NetworkX and Matplotlib."""
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
        """Check if the graph is acyclic (contains no cycles)."""
        node_ids = list(self.internal_nodes.progression)
        check_deque = deque(node_ids)

        # 0: unvisited, 1: visiting, 2: visited
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
        """Return nodes with no outgoing edges (tail/sink nodes)."""
        result = []
        for node_id in self.node_edge_mapping:
            if self.node_edge_mapping[node_id]["out"] == {}:
                result.append(self.internal_nodes[node_id])
        return Pile(result, item_type={Node}, strict_type=False)

    def topological_sort(self) -> list[Node]:
        """Topological sort via Kahn's algorithm.

        Returns:
            list[Node]: Nodes in topological order.

        Raises:
            ValueError: If graph contains cycles.
        """
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
        """Find path between two nodes via BFS.

        Args:
            start: Source node or node ID.
            end: Target node or node ID.
            check_conditions: If True, respect edge conditions during traversal.

        Returns:
            list[Edge] if path exists, None otherwise.

        Raises:
            RelationError: If start or end node not in graph.
        """
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
                        # Reconstruct path
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
        """Replace a node, transferring all edges to the new node.

        The new node inherits the old node's exact graph position —
        all incoming edges now point to ``new_node``, all outgoing
        edges now originate from ``new_node``. The old node is
        removed from the graph and returned.

        Args:
            old: The node or node ID to replace.
            new_node: The replacement node (must not already be in the graph).

        Returns:
            The removed old node.
        """
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
        """Insert a node between ``anchor`` and all of its successors.

        Before: ``anchor -> s1``, ``anchor -> s2``
        After:  ``anchor -> new_node -> s1``, ``new_node -> s2``

        The original edges from ``anchor`` to its successors are
        removed; new edges are created to route through ``new_node``.
        Edge ``condition``, ``label``, and any extra custom
        ``properties`` on the original edges are preserved.

        Args:
            anchor: The node or node ID to splice after.
            new_node: The node to insert (must not already be in the graph).

        Returns:
            List of newly created edges. The first entry is the
            ``anchor -> new_node`` link; the rest are the
            ``new_node -> successor`` edges.
        """
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
