from pydantic import Field

from lionagi.os.primitives.node import Node
from lionagi.os.primitives.graph.graph import Graph


class Tree(Graph):

    root: Node | None = Field(
        default=None, description="The root node of the tree graph."
    )


__all__ = ["Tree"]
