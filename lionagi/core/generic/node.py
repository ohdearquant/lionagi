from typing import Any, Type

from pydantic import Field
from lionagi.integrations.bridge import LlamaIndexBridge, LangchainBridge
from .abc import BaseNode, Condition
from .edge import Edge
from .relations import Relations
from .mail import MailBox


class Node(BaseNode):


    relations: Relations = Field(
        default_factory=Relations,
        description="The relations of the node.",
        validation_alias="node_relations",
    )

    mailbox: MailBox = Field(
        default_factory=MailBox,
        description="The mailbox for incoming and outgoing mails.",
    )

    @property
    def related_nodes(self) -> list[str]:
        """Returns a set of node IDs related to this node, excluding itself."""
        nodes = self.relations.relevant_nodes
        nodes.discard(self.id_)
        return list(nodes)

    @property
    def edges(self) -> dict[str, Edge]:
        """Returns a dictionary of all edges connected to this node."""
        return self.relations.edges

    @property
    def node_relations(self) -> dict:
        """Categorizes preceding and succeeding relations to this node."""
        
        out_nodes: dict[str, list[str]] = {}
        for edge in self.relations.right.pile.values():
            for i in self.related_nodes:
                if edge.tail == i:
                    if i in out_nodes:
                        out_nodes[i].append(edge)
                    else:
                        out_nodes[i] = [edge]

        in_nodes: dict[str, list[str]] = {}
        for edge in self.relations.left.pile.values():
            for i in self.related_nodes:
                if edge.head == i:
                    if i in in_nodes:
                        in_nodes[i].append(edge)
                    else:
                        in_nodes[i] = [edge]

        return {"out": out_nodes, "in_": in_nodes}

    @property
    def precedessors(self) -> list[str]:
        """return a list of nodes id that precede this node"""
        return [k for k, v in self.node_relations["in_"].items() if len(v) > 0]

    @property
    def successors(self) -> list[str]:
        """return a list of nodes id that succeed this node"""
        return [k for k, v in self.node_relations["out"].items() if len(v) > 0]

    def relate(
        self,
        node: "Node",
        direction="out",
        condition: Condition | None = None,
        label: str | None = None,
        bundle=False,
    ) -> None:
        if direction == "out":
            edge = Edge(
                head=self, tail=node, condition=condition, bundle=bundle, label=label
            )
            self.relations.append(edge, direction)
            node.relations.append(edge, "in")

        elif direction == "in":
            edge = Edge(
                head=node, tail=self, condition=condition, label=label, bundle=bundle
            )
            self.relations.append(edge, direction)
            node.relations.append(edge, "out")

        else:
            raise ValueError(
                f"Invalid value for self_as: {direction}, must be 'in' or 'out'"
            )

    def remove_edge(self, node: "Node", edge: Edge | str) -> bool:
        if node.id_ not in self.related_nodes:
            raise ValueError(f"Node {self.id_} is not related to node {node.id_}.")

        edge_id = edge.id_ if isinstance(edge, Edge) else edge

        if (
            edge_id not in self.relations.edges 
            or edge_id not in node.relations.edges
        ):
            raise ValueError(
                f"Edge {edge_id} does not exist between nodes {self.id_} and {node.id_}."
            )
            
        try:
            self.relations.pop(edge_id)
            node.relations.pop(edge_id)
            return True

        except Exception as e:
            raise ValueError(
                f"Failed to remove edge between nodes {self.id_} and {node.id_}."
            ) from e

    def unrelate(self, node: "Node", edge: Edge | str = "all") -> bool:
        """
        Removes one or all relations between this node and another.

        Args:
            node (Node): The node to unrelate from.
            edge (Edge | str): Specific edge or 'all' to remove all relations.
                Defaults to "all".

        Returns:
            bool: True if the operation is successful, False otherwise.

        Raises:
            ValueError: If the node is not related or the edge does not exist.
        """
        if edge == "all":
            edge = self.node_relations["out"].get(
                node.id_, []) + self.node_relations["in_"].get(node.id_, [])
        else:
            edge = [edge.id_] if isinstance(edge, Edge) else [edge]

        if len(edge) == 0:
            raise ValueError(f"Node {self.id_} is not related to node {node.id_}.")

        try:
            for edge_id in edge:
                self.remove_edge(node, edge_id)
            return True
        except Exception as e:
            raise ValueError(
                f"Failed to remove edge between nodes {self.id_} and " f"{node.id_}."
            ) from e

    def to_llama_index(self, node_type: Type | str | Any = None, **kwargs) -> Any:
        """
        Serializes this node for LlamaIndex.

        Args:
            node_type (Type | str | Any): The type of node in LlamaIndex.
                Defaults to None.
            **kwargs: Additional keyword arguments for serialization.

        Returns:
            Any: The serialized node for LlamaIndex.
        """
        return LlamaIndexBridge.to_llama_index_node(self, node_type=node_type, **kwargs)

    def to_langchain(self, **kwargs) -> Any:
        """
        Serializes this node for Langchain.

        Args:
            **kwargs: Additional keyword arguments for serialization.

        Returns:
            Any: The serialized node for Langchain.
        """
        return LangchainBridge.to_langchain_document(self, **kwargs)

    def __str__(self) -> str:
        """
        Provides a string representation of the node.

        Returns:
            str: The string representation of the node.
        """
        timestamp = f" ({self.timestamp})" if self.timestamp else ""
        if self.content:
            content_preview = (
                f"{self.content[:50]}..." if len(self.content) > 50 else self.content
            )
        else:
            content_preview = ""
        meta_preview = (
            f"{str(self.metadata)[:50]}..."
            if len(str(self.metadata)) > 50
            else str(self.metadata)
        )
        return (
            f"{self.class_name()}({self.id_}, {content_preview}, {meta_preview},"
            f"{timestamp})"
        )
