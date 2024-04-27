"""
This module provides the BaseStructure class, which represents a node with
internal edges and nodes, including methods for managing them.

The BaseStructure class inherits from the Node class and provides functionality
for adding, removing, and querying nodes and edges within the structure, as
well as checking if the structure is empty.
"""

from functools import singledispatchmethod
from typing import Any
from pydantic import Field

from lionagi.libs import convert, func_call

from ..generic.abc import Condition
from ..generic import Edge, Node
from ..action.node import ActionSelection
from ..action.tool import Tool


class BaseStructure(Node):
    """
    Represents a node with internal edges and nodes, including methods for
    managing them.

    Provides functionality for adding, removing, and querying nodes and edges
    within the structure, as well as checking if the structure is empty.
    """

    internal_nodes: dict[str, Node] = Field(
        default_factory=dict,
        description="A dictionary of all nodes within the structure, keyed by \
            node ID.",
    )

    @property
    def internal_edges(self) -> dict[str, Edge]:
        """
        Gets all internal edges indexed by their ID.

        Returns:
            dict[str, Edge]: A dictionary of all internal edges within the
            structure.
        """
        edges = {}
        for i in self.internal_nodes.values():
            for _, edge in i.edges.items():
                if edge.id_ not in edges:
                    edges[edge.id_] = edge
        return edges

    @property
    def is_empty(self) -> bool:
        """
        Checks if the structure is empty (contains no nodes).

        Returns:
            bool: True if the structure has no nodes, False otherwise.
        """
        return len(self.internal_nodes) == 0

    def clear(self):
        """Clears all nodes and edges from the structure."""
        self.internal_nodes.clear()

    def get_node_edges(
        self,
        node: Node | str,
        node_as: str = "both",
        label: list | str = None,
    ) -> dict[str, list[Edge]]:
        """
        Retrieves edges associated with a specific node.

        Args:
            node (Node | str): The node or its ID to query for edges.
            node_as (str, optional): The role of the node in the edges.
                Defaults to "both".
                - "both" or "all": Retrieve edges where the node is either the
                  head or tail.
                - "head", "predecessor", "outgoing", "out", or "predecessors":
                  Retrieve edges where the node is the head.
                - "tail", "successor", "incoming", "in", or "successors":
                  Retrieve edges where the node is the tail.
            label (list | str, optional): Filter edges by label(s). Defaults to
                None.

        Returns:
            dict[str, list[Edge]]: A dictionary mapping related node IDs to
            lists of associated edges based on the specified criteria.

        Raises:
            ValueError: If an invalid role is specified for the node.
        """

        node = self.get_node(node)

        if node_as in ["all", "both"]:
            all_node_edges = {i: [] for i in node.related_nodes}

            for k, v in node.node_relations["points_to"].items():
                all_node_edges[k].append(v)

            for k, v in node.node_relations["pointed_by"].items():
                all_node_edges[k].append(v)

            for k, v in all_node_edges.items():
                all_node_edges[k] = convert.to_list(v, dropna=True, flatten=True)

            if label:
                for k, v in all_node_edges.items():
                    all_node_edges[k] = (
                        v
                        if v.label in convert.to_list(label, dropna=True, flatten=True)
                        else []
                    )

            return {k: v for k, v in all_node_edges.items() if len(v) > 0}

        elif node_as in ["head", "predecessor", "outgoing", "out", "predecessors"]:
            return node.node_relations["points_to"]

        elif node_as in ["tail", "successor", "incoming", "in", "successors"]:
            return node.node_relations["pointed_by"]

        else:
            raise ValueError(
                f"Invalid value for self_as: {node_as}, must be 'head' or 'tail'"
            )

    def relate_nodes(
        self,
        head: Node,
        tail: Node,
        condition: Condition | None = None,
        bundle=False,
        label=None,
        **kwargs,
    ):
        """
        Relates two nodes within the structure with an edge.

        Args:
            head (Node): The head node of the edge.
            tail (Node): The tail node of the edge.
            condition (Condition | None, optional): The condition associated
                with the edge. Defaults to None.
            label (optional): The label for the edge.
            **kwargs: Additional keyword arguments for the edge creation.

        Raises:
            ValueError: If there is an error adding the edge.
        """
        try:
            if isinstance(head, Tool) or isinstance(head, ActionSelection):
                raise ValueError(
                    f"type {type(tail)} should not be the head of the relationship, "
                    f"please switch position and attach it to the tail of the relationship"
                )

            if isinstance(tail, Tool) or isinstance(tail, ActionSelection):
                bundle = True

            if head.id_ not in self.internal_nodes:
                self.add_node(head)

            if tail.id_ not in self.internal_nodes:
                self.add_node(tail)

            head.relate(
                tail,
                node_as="head",
                condition=condition,
                label=label,
                bundle=bundle,
                **kwargs,
            )
        except Exception as e:
            raise ValueError(f"Error adding edge: {e}") from e

    def remove_edge(
        self, edge: Edge | str | list[str | Edge] | dict[str, Edge]
    ) -> bool:
        """
        Removes one or more edges from the structure.

        Args:
            edge: The edge(s) to be removed, specified as single edge, its ID,
                a list, or a dictionary of edges.

        Returns:
            bool: True if all specified edges were successfully removed, False
            otherwise.
        """
        if isinstance(edge, list):
            for i in edge:
                self._remove_edge(i)

        elif isinstance(edge, dict):
            for i in edge.values():
                self._remove_edge(i)

        elif isinstance(edge, (Edge, str)):
            self._remove_edge(edge)

    @singledispatchmethod
    def add_node(self, node: Any) -> None:
        """
        Method placeholder for adding a node. Will be implemented by register
        decorators.

        Args:
            node (Any): The node to be added.

        Raises:
            NotImplementedError: If the method is not implemented for the type
            of node.
        """
        raise NotImplementedError

    @add_node.register(Node)
    def _(self, node) -> None:
        if node.id_ not in self.internal_nodes:
            self.internal_nodes[node.id_] = node
        else:
            raise ValueError(f"Node {node.id_} already exists in structure.")

    @add_node.register(list)
    def _(self, node) -> None:
        for _node in node:
            self.add_node(_node)

    @add_node.register(dict)
    def _(self, node) -> None:
        self.add_node(list(node.values()))

    def get_node(self, node, default="undefined"):
        """
        Retrieves one or more nodes by ID, node instance, or a collection of
        IDs/nodes.

        Args:
            node: A single node, node ID, or a collection of nodes/IDs to
                retrieve.
            default: The default value to return if a node is not found.

        Returns:
            The node(s) corresponding to the input, or the default value for
            any not found.
        """
        if not isinstance(node, (list, dict, set)):
            return self._get_node(node, default=default)
        else:
            node = list(node) if isinstance(node, set) else node
            node = list(node.values()) if isinstance(node, dict) else node

            return func_call.lcall(node, self._get_node, default=default)

    @singledispatchmethod
    def remove_node(self, node: Any) -> bool:
        """
        Method placeholder for removing a node. Will be implemented by register
        decorators.

        Args:
            node (Any): The node to be removed.

        Returns:
            bool: True if the node was successfully removed, False otherwise.

        Raises:
            NotImplementedError: If the method is not implemented for the type
            of node.
        """
        return NotImplementedError

    @remove_node.register(Node)
    def _(self, node: Node) -> bool:
        return self._remove_node(node)

    @remove_node.register(str)
    def _(self, node: str) -> bool:
        return self._remove_node(node)

    @remove_node.register(list)
    def _(self, node: list[str | Node]) -> bool:
        for i in node:
            self.remove_node(i)

    @remove_node.register(dict)
    def _(self, node: dict[str, Node]) -> bool:
        self.remove_node(list(node.values()))

    def _pop_node(self, node: Node | str, default="undefined"):
        node = self.get_node(node, default=default)
        self.remove_node(node)
        return node

    @singledispatchmethod
    def pop_node(self, node: Any, default="undefined") -> Node:
        return NotImplementedError

    @pop_node.register(Node)
    def _(self, node: Node, default="undefined") -> Node:
        return self._pop_node(node, default=default)

    @pop_node.register(str)
    def _(self, node: str, default="undefined") -> Node:
        return self._pop_node(node, default=default)

    @pop_node.register(list)
    def _(self, node: list[str | Node], default="undefined") -> list[Node]:
        nodes = []
        for i in node:
            nodes.append(self.pop_node(i, default=default))
        return nodes

    @pop_node.register(dict)
    def _(self, node: dict[str, Node], default="undefined") -> list[Node]:
        return self.pop_node(list(node.values()), default=default)

    def _remove_edge(self, edge: Edge | str) -> bool:
        """Helper method to remove an edge by ID or edge instance.

        Args:
            edge (Edge | str): The edge or its ID to remove.

        Returns:
            bool: True if the edge was successfully removed, False otherwise.

        Raises:
            ValueError: If the edge is not found within the structure.
        """
        edge_id = edge.id_ if isinstance(edge, Edge) else edge
        if not edge_id in self.internal_edges:
            raise ValueError(f"Edge {edge_id} not found in structure.")

        edge = self.internal_edges[edge_id]

        head: Node = self.get_node(edge.head)
        tail: Node = self.get_node(edge.tail)

        head.unrelate(tail, edge=edge)

    def _get_node(self, node: Node | str, default="undefined") -> Node:
        node_id_ = node.id_ if isinstance(node, Node) else node
        if not node_id_ in self.internal_nodes:
            if default == "undefined":
                raise KeyError(f"Node {node_id_} not found in structure.")
            return default
        return self.internal_nodes[node_id_]

    def _remove_node(self, node: Node | str) -> bool:
        try:
            node = self.get_node(node)
            related_nodes = self.get_node(node.related_nodes)
            func_call.lcall(related_nodes, node.unrelate)
            self.internal_nodes.pop(node.id_)
            return True
        except Exception as e:
            raise ValueError(f"Error removing node: {e}") from e
