"""
A module for representing relationships between nodes in a graph structure, 
encapsulating incoming and outgoing edges.
"""

from pydantic import Field
from pydantic.dataclasses import dataclass
from lionagi.libs import convert
from .edge import Edge


@dataclass
class Relations:
    """
    Represents the relationships of a node via its incoming and outgoing edges.

    This class stores edges in two dictionaries: `preceding` for outgoing edges
    and `succeeding` for incoming edges. It provides properties to access all
    edges together and to get a unique set of all connected node IDs.

    Attributes:
        preceding (dict[str, Edge]): A dictionary of outgoing edges from the
            node, with the edge ID as the key and the `Edge` object as the
            value. Represents edges leading from this node to other nodes.
        succeeding (dict[str, Edge]): A dictionary of incoming edges to the
            node, with the edge ID as the key and the `Edge` object as the
            value. Represents edges from other nodes leading to this node.
    """

    points_to: dict[str, Edge] = Field(
        title="Outgoing edges",
        default_factory=dict,
        description="The Outgoing edges of the node, reads self precedes other, \
            {edge_id: Edge}",
    )

    pointed_by: dict[str, Edge] = Field(
        title="Incoming edges",
        default_factory=dict,
        description="The Incoming edges of the node, reads self succeeds other, \
            {edge_id: Edge}",
    )

    @property
    def all_edges(self) -> dict[str, Edge]:
        """
        Combines and returns all incoming and outgoing edges of the node.

        Returns:
            dict[str, Edge]: A dictionary of all edges connected to the node,
                including both preceding (outgoing) and succeeding (incoming)
                edges, indexed by edge IDs.
        """
        return {**self.points_to, **self.pointed_by}

    @property
    def all_nodes(self) -> set[str]:
        """
        Extracts and returns a unique set of all node IDs connected to this
        node through its edges.

        It processes both heads and tails of each edge in `all_edges`, flattens
        the list to a one-dimensional list, and then converts it to a set to
        ensure uniqueness.

        Returns:
            set[str]: A set of unique node IDs connected to this node, derived
                from both incoming and outgoing edges.
        """
        return set(convert.to_list([[i.head, i.tail] for i in self.all_edges.values()]))
