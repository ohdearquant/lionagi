from lion_core.graph.graph import Graph as CoreGraph


from lionagi.os.primitives.node import Node
from lionagi.os.primitives.utils import core_to_lionagi_container


class Graph(CoreGraph, Node):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.internal_nodes = core_to_lionagi_container(self.internal_nodes)
        self.internal_edges = core_to_lionagi_container(self.internal_edges)
        self.node_edge_mapping = core_to_lionagi_container(self.node_edge_mapping)


__all__ = ["Graph"]
