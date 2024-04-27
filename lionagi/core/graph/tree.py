"""
This module provides classes for representing and working with tree structures.

The module includes the following classes:
- TreeLabel: An enumeration representing tree relationships (parent and child).
- TreeNode: A specialized node representing a node within a tree structure,
  extending the basic node functionality with parent-child relationships.
- Tree: Represents a tree structure, extending the graph with tree-specific
  functionalities like managing parent-child relationships.
"""

from enum import Enum
from pydantic import Field

from lionagi.core.generic import Node, Condition
from lionagi.core.graph.graph import Graph


class TreeLabel(str, Enum):
    """
    Enumeration representing tree relationships.

    Attributes:
        PARENT (str): Represents the parent edge.
        CHILD (str): Represents the child edge.
    """

    PARENT = "parent"
    CHILD = "child"


class TreeNode(Node):
    """
    A specialized node representing a node within a tree structure.

    Extends the basic node functionality with parent-child relationships.

    Attributes:
        parent (Node | None): The parent node. Defaults to None if the node has
            no parent.
    """

    parent: Node | None = Field(
        default=None,
        description="The parent node, as an instance of Node.",
    )

    @property
    def children(self) -> list[str]:
        """
        Retrieves the IDs of all child nodes.

        Returns:
            list[str]: A list containing the IDs of the child nodes.
        """
        if not self.parent:
            return list(self.related_nodes)
        else:
            return [
                node_id for node_id in self.related_nodes if node_id != self.parent.id_
            ]

    def relate_child(
        self,
        child: Node | list[Node],
        condition: Condition | None = None,
        bundle: bool = False,
    ) -> None:
        """
        Establishes a parent-child relationship between this node and the given
        child node(s).

        Args:
            child (Node | list[Node]): The child node or list of child nodes to
                be related.
            condition (Condition | None): The condition associated with the
                relationship, if any.
            bundle (bool): Indicates whether to bundle the relation into a
                single transaction. Defaults to False.
        """
        children = [child] if isinstance(child, Node) else child
        for _child in children:
            self.relate(
                _child,
                node_as="head",
                label=TreeLabel.PARENT,
                condition=condition,
                bundle=bundle,
            )
            if isinstance(_child, TreeNode):
                _child.parent = self

    def relate_parent(
        self,
        parent: Node,
        condition: Condition | None = None,
        bundle: bool = False,
    ) -> None:
        """
        Establishes a parent-child relationship between the given parent node
        and this node.

        Args:
            parent (Node): The parent node to be related.
            condition (Condition | None): The condition associated with the
                relationship, if any.
            bundle (bool): Indicates whether to bundle the relation into a
                single transaction. Defaults to False.
        """
        if self.parent:
            self.unrelate(self.parent)
        self.relate(
            parent,
            node_as="tail",
            label=TreeLabel.PARENT,
            condition=condition,
            bundle=bundle,
        )
        self.parent = parent

    def unrelate_parent(self):
        """
        Removes the parent relationship of this node.
        """
        self.unrelate(self.parent)
        self.parent = None

    def unrelate_child(self, child: Node | list[Node]):
        """
        Removes the child relationship between this node and the given child
        node(s).

        Args:
            child (Node | list[Node]): The child node or list of child nodes to
                be unrelated.
        """
        children: list[Node] = [child] if isinstance(child, Node) else child
        for _child in children:
            self.unrelate(_child)
            if isinstance(_child, TreeNode):
                _child.parent = None


class Tree(Graph):
    """
    Represents a tree structure, extending the graph with tree-specific
    functionalities.

    Manages parent-child relationships within the tree.

    Attributes:
        root (TreeNode | None): The root node of the tree. Defaults to None.
    """

    root: TreeNode | None = Field(
        default=None, description="The root node of the tree graph."
    )

    def relate_parent_child(
        self,
        parent: TreeNode,
        children: list[TreeNode],
        condition: Condition | None = None,
        bundle: bool = False,
    ) -> None:
        """
        Establishes parent-child relationships between the given parent node and
        child node(s).

        Args:
            parent (TreeNode): The parent node.
            children (list[TreeNode]): A list of child nodes.
            condition (Condition | None): The condition associated with the
                relationships, if any.
            bundle (bool): Indicates whether to bundle the relations into a
                single transaction. Defaults to False.
        """
        children = [children] if isinstance(children, TreeNode) else children
        for child in children:
            if child.id_ not in self.internal_nodes:
                self.add_node(child)

        for child in children:
            parent.relate_child(child, condition=condition, bundle=bundle)

        if parent.id_ not in self.internal_nodes:
            self.add_node(parent)

        if self.root is None:
            self.root = parent
