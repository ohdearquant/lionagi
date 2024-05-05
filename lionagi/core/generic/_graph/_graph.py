from collections import deque
from typing import Any
from pydantic import Field

from lionagi.libs.ln_convert import to_list
from ..abc import Condition, Actionable, LionTypeError, ItemNotFoundError, LionIDable
from .._edge._edge import Edge
from .._node._node import Node
from .._pile import Pile, pile


class Graph(Node):

    internal_nodes: Pile = pile()

    @property
    def internal_edges(self) -> Pile[Edge]:
        return pile(
            {edge.ln_id: edge for node in self.internal_nodes for edge in node.edges},
            Edge,
        )

    def is_empty(self) -> bool:
        return self.internal_nodes.is_empty()

    def clear(self):
        self.internal_nodes.clear()

    def add_edge(
        self,
        head: Node,
        tail: Node,
        condition: Condition | None = None,
        bundle=False,
        label=None,
        **kwargs,
    ):
        """
        relate and include nodes in the structure
        """
        if isinstance(head, Actionable):
            raise LionTypeError("Actionable nodes cannot be related as head.")
        if isinstance(tail, Actionable):
            bundle = True

        self.internal_nodes.include(head)
        self.internal_nodes.include(tail)

        head.relate(
            tail,
            direction="out",
            condition=condition,
            label=label,
            bundle=bundle,
            **kwargs,
        )

    def remove_edge(self, edge: Any) -> bool:
        if all(self._remove_edge(i) for i in edge):
            return True
        return False

    def add_node(self, node: Any) -> None:
        self.internal_nodes.update(node)

    def get_node(self, item: LionIDable, default=...):
        return self.internal_nodes.get(item, default)

    def get_node_edges(
        self,
        node: Node | str,
        direction: str = "both",
        label: list | str = None,
    ) -> Pile[Edge] | None:

        node = self.internal_nodes[node]
        edges = None
        match direction:
            case "both":
                edges = node.edges
            case "head", "predecessor", "outgoing", "out", "predecessors":
                edges = node.relations["out"]
            case "tail", "successor", "incoming", "in", "successors":
                edges = node.relations["in"]

        if label:
            return (
                pile(
                    [
                        edge
                        for edge in edges
                        if edge.label in to_list(label, dropna=True, flatten=True)
                    ]
                )
                if edges
                else None
            )
        return pile(edges) if edges else None

    def pop_node(self, item, default: ..., /):
        return self.internal_nodes.pop(item, default)

    def remove_node(self, item, /):
        return self.internal_nodes.remove(item)

    def _remove_edge(self, edge: Edge | str) -> bool:

        if edge not in self.internal_edges:
            raise ItemNotFoundError(f"Edge {edge} does not exist in structure.")

        edge = self.internal_edges[edge]
        head: Node = self.internal_nodes[edge.head]
        tail: Node = self.internal_nodes[edge.tail]

        head.unrelate(tail, edge=edge)

    def get_heads(self) -> Pile:
        return pile(
            [
                node
                for node in self.internal_nodes
                if node.relations["in"].is_empty() and not isinstance(node, Actionable)
            ]
        )

    def is_acyclic(self) -> bool:
        """
        Checks if the graph is acyclic.

        An acyclic graph contains no cycles and can be represented as a directed
        acyclic graph (DAG).

        Returns:
            bool: True if the graph is acyclic, False otherwise.
        """
        node_ids = list(self.internal_nodes.keys())
        check_deque = deque(node_ids)
        check_dict = {key: 0 for key in node_ids}  # 0: not visited, 1: temp, 2: perm

        def visit(key):
            if check_dict[key] == 2:
                return True
            elif check_dict[key] == 1:
                return False

            check_dict[key] = 1

            for edge in self.internal_nodes[key].relations["out"]:
                check = visit(edge.tail)
                if not check:
                    return False

            check_dict[key] = 2
            return True

        while check_deque:
            key = check_deque.pop()
            check = visit(key)
            if not check:
                return False
        return True

    def to_networkx(self, **kwargs) -> Any:
        """
        Converts the graph into a NetworkX graph object.

        The NetworkX graph object can be used for further analysis or
        visualization.

        Args:
            **kwargs: Additional keyword arguments to pass to the NetworkX graph
                constructor.

        Returns:
            Any: A NetworkX graph object representing the current graph
            structure.
        """
        from lionagi.libs import SysUtil

        SysUtil.check_import("networkx")

        from networkx import DiGraph

        g = DiGraph(**kwargs)
        for node in self.internal_nodes:
            node_info = node.to_dict()
            node_info.pop("ln_id")
            node_info.update({"class_name": node.class_name()})
            g.add_node(node.ln_id, **node_info)

        for _edge in self.internal_edges:
            edge_info = _edge.to_dict()
            edge_info.pop("ln_id")
            edge_info.update({"class_name": _edge.class_name()})
            source_node_id = edge_info.pop("head")
            target_node_id = edge_info.pop("tail")
            g.add_edge(source_node_id, target_node_id, **edge_info)

        return g

    def display(self, **kwargs):
        """
        Displays the graph using NetworkX's drawing capabilities.

        This method requires NetworkX and a compatible plotting library (like
        matplotlib) to be installed.

        Args:
            **kwargs: Additional keyword arguments to pass to the NetworkX graph
                constructor.
        """
        from lionagi.libs import SysUtil

        SysUtil.check_import("networkx")
        SysUtil.check_import("matplotlib", "pyplot")

        import networkx as nx
        import matplotlib.pyplot as plt

        g = self.to_networkx(**kwargs)
        pos = nx.spring_layout(g)
        nx.draw(
            g,
            pos,
            edge_color="black",
            width=1,
            linewidths=1,
            node_size=500,
            node_color="orange",
            alpha=0.9,
            labels=nx.get_node_attributes(g, "class_name"),
        )

        labels = nx.get_edge_attributes(g, "label")
        labels = {k: v for k, v in labels.items() if v}

        if labels:
            nx.draw_networkx_edge_labels(
                g, pos, edge_labels=labels, font_color="purple"
            )

        plt.axis("off")
        plt.show()

    def size(self) -> int:
        return len(self.internal_nodes)