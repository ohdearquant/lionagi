from typing import Any, Type
from pydantic import Field
from lionagi.integrations.bridge import LlamaIndexBridge, LangchainBridge

from lionagi.core.generic.abstract.component import BaseNode
from lionagi.core.generic.abstract.condition import Condition
from lionagi.core.generic.edge import Edge
from lionagi.core.generic.relation import Relations
from lionagi.core.generic.mailbox import MailBox


class Node(BaseNode):
    """
    Represents a node with relations to other nodes.

    Attributes:
        relations (Relations): The relations of the node, managed through a
            `Relations` instance.

    Properties:
        related_nodes: A set of IDs representing nodes related to this node.
        edges: A dictionary of all edges connected to this node.
        node_relations: A dictionary categorizing preceding and succeeding
            relations to this node.
        precedessors: A list of node IDs that precede this node.
        successors: A list of node IDs that succeed this node.

    Methods:
        relate(node, self_as, condition, **kwargs): Relates this node to
            another node with an edge.
        unrelate(node, edge): Removes one or all relations between this node
            and another.
        to_llama_index(node_type, **kwargs): Serializes this node for
            LlamaIndex.
        to_langchain(**kwargs): Serializes this node for Langchain.
        from_llama_index(llama_node, **kwargs): Deserializes a node from
            LlamaIndex data.
        from_langchain(lc_doc): Deserializes a node from Langchain data.
        __str__(): String representation of the node.

    Raises:
        ValueError: When invalid parameters are provided to methods.
    """

    relations: Relations = Field(
        default_factory=Relations,
        description="The relations of the node.",
        alias="node_relations",
    )

    mailbox: MailBox = Field(
        default_factory=MailBox,
        description="The mailbox for incoming and outgoing mails.",
    )

    @property
    def related_nodes(self) -> list[str]:
        """Returns a set of node IDs related to this node, excluding itself."""
        nodes = set(self.relations.all_nodes)
        nodes.discard(self.id_)
        return list(nodes)

    @property
    def edges(self) -> dict[str, Edge]:
        """Returns a dictionary of all edges connected to this node."""
        return self.relations.all_edges

    @property
    def node_relations(self) -> dict:
        """Categorizes preceding and succeeding relations to this node."""

        points_to_nodes = {}
        for edge in self.relations.points_to.values():
            for i in self.related_nodes:
                if edge.tail == i:
                    if i in points_to_nodes:
                        points_to_nodes[i].append(edge)
                    else:
                        points_to_nodes[i] = [edge]

        pointed_by_nodes = {}
        for edge in self.relations.pointed_by.values():
            for i in self.related_nodes:
                if edge.head == i:
                    if i in pointed_by_nodes:
                        pointed_by_nodes[i].append(edge)
                    else:
                        pointed_by_nodes[i] = [edge]

        return {"points_to": points_to_nodes, "pointed_by": pointed_by_nodes}

    @property
    def precedessors(self) -> list[str]:
        """return a list of nodes id that precede this node"""
        return [k for k, v in self.node_relations["pointed_by"].items() if len(v) > 0]

    @property
    def successors(self) -> list[str]:
        """return a list of nodes id that succeed this node"""
        return [k for k, v in self.node_relations["points_to"].items() if len(v) > 0]

    def relate(
        self,
        node: "Node",
        node_as: str = "head",
        condition: Condition | None = None,
        label: str | None = None,
        bundle=False,
    ) -> None:
        """Relates this node to another node with an edge.

        Args:
            node (Node): The node to relate to.
            self_as (str): Specifies whether this node is the 'head' or 'tail'
                of the relation. Defaults to "head".
            condition (Condition | None): The condition associated with the
                edge, if any. Defaults to None.
            **kwargs: Additional keyword arguments for edge creation.

        Raises:
            ValueError: If `self_as` is not 'head' or 'tail'.
        """
        if node_as == "head":
            edge = Edge(
                head=self, tail=node, condition=condition, bundle=bundle, label=label
            )
            self.relations.points_to[edge.id_] = edge
            node.relations.pointed_by[edge.id_] = edge

        elif node_as == "tail":
            edge = Edge(
                head=node, tail=self, condition=condition, label=label, bundle=bundle
            )
            self.relations.pointed_by[edge.id_] = edge
            node.relations.points_to[edge.id_] = edge

        else:
            raise ValueError(
                f"Invalid value for self_as: {node_as}, must be 'head' or 'tail'"
            )

    def remove_edge(self, node: "Node", edge: Edge | str) -> bool:
        if node.id_ not in self.related_nodes:
            raise ValueError(f"Node {self.id_} is not related to node {node.id_}.")

        edge_id = edge.id_ if isinstance(edge, Edge) else edge

        if (
            edge_id not in self.relations.all_edges
            or edge_id not in node.relations.all_edges
        ):
            raise ValueError(
                f"Edge {edge_id} does not exist between nodes {self.id_} and "
                f"{node.id_}."
            )

        all_dicts = [
            self.relations.points_to,
            self.relations.pointed_by,
            node.relations.points_to,
            node.relations.pointed_by,
        ]
        try:
            for _dict in all_dicts:
                edge_id = edge.id_ if isinstance(edge, Edge) else edge
                _dict.pop(edge_id, None)
            return True

        except Exception as e:
            raise ValueError(
                f"Failed to remove edge between nodes {self.id_} and " f"{node.id_}."
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
            edge = self.node_relations["points_to"].get(
                node.id_, []
            ) + self.node_relations["pointed_by"].get(node.id_, [])
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

    @classmethod
    def from_llama_index(cls, llama_node: Any, **kwargs) -> "Node":
        """
        Deserializes a node from LlamaIndex data.

        Args:
            llama_node (Any): The LlamaIndex node data.
            **kwargs: Additional keyword arguments for deserialization.

        Returns:
            Node: The deserialized node.
        """
        llama_dict = llama_node.to_dict(**kwargs)
        return cls.from_obj(llama_dict)

    @classmethod
    def from_langchain(cls, lc_doc: Any) -> "Node":
        """Deserializes a node from Langchain data.

        Args:
            lc_doc (Any): The Langchain document data.

        Returns:
            Node: The deserialized node.
        """
        langchain_json = lc_doc.to_json()
        langchain_dict = {"lc_id": langchain_json["id"], **langchain_json["kwargs"]}
        return cls.from_obj(langchain_dict)

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
