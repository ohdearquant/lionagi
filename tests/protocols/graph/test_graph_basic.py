import pytest

from lionagi._errors import RelationError
from lionagi.protocols.types import Edge, Graph

from .helpers import create_test_node


@pytest.fixture
def empty_graph():
    """Fixture for empty graph"""
    return Graph()


@pytest.fixture
def simple_graph():
    """Fixture for simple graph with two connected nodes"""
    graph = Graph()

    # Create nodes
    node1 = create_test_node("Node1")
    node2 = create_test_node("Node2")

    # Add nodes
    graph.add_node(node1)
    graph.add_node(node2)

    # Create and add edge
    edge = Edge(head=node1, tail=node2)
    graph.add_edge(edge)

    return graph, node1, node2, edge


@pytest.fixture
def complex_graph():
    """Fixture for complex graph with multiple nodes and edges"""
    graph = Graph()

    # Create nodes
    nodes = [create_test_node(f"Node{i}") for i in range(4)]

    # Add nodes
    for node in nodes:
        graph.add_node(node)

    # Create edges
    edges = [
        Edge(head=nodes[0], tail=nodes[1]),  # 0 -> 1
        Edge(head=nodes[1], tail=nodes[2]),  # 1 -> 2
        Edge(head=nodes[2], tail=nodes[3]),  # 2 -> 3
        Edge(head=nodes[0], tail=nodes[3]),  # 0 -> 3
    ]

    # Add edges
    for edge in edges:
        graph.add_edge(edge)

    return graph, nodes, edges


class TestGraphBasics:
    def test_empty_graph_creation(self, empty_graph):
        assert len(empty_graph.internal_nodes) == 0
        assert len(empty_graph.internal_edges) == 0
        assert isinstance(empty_graph.node_edge_mapping, dict)

    def test_add_node(self, empty_graph):
        node = create_test_node("TestNode")
        empty_graph.add_node(node)
        assert node.id in empty_graph.internal_nodes
        assert empty_graph.node_edge_mapping[node.id] == {
            "in": {},
            "out": {},
        }

    def test_add_invalid_node(self, empty_graph):
        with pytest.raises(RelationError):
            empty_graph.add_node("not a node")

    def test_add_duplicate_node(self, empty_graph):
        node = create_test_node("TestNode")
        empty_graph.add_node(node)
        with pytest.raises(RelationError):
            empty_graph.add_node(node)

    def test_add_edge(self, simple_graph):
        graph, node1, node2, edge = simple_graph
        assert edge.id in graph.internal_edges
        assert graph.node_edge_mapping[node1.id]["out"][edge.id] == node2.id
        assert graph.node_edge_mapping[node2.id]["in"][edge.id] == node1.id

    def test_add_invalid_edge(self, empty_graph):
        with pytest.raises(RelationError):
            empty_graph.add_edge("not an edge")

    def test_add_edge_missing_nodes(self, empty_graph):
        node1 = create_test_node("Node1")
        node2 = create_test_node("Node2")
        edge = Edge(head=node1, tail=node2)
        with pytest.raises(RelationError):
            empty_graph.add_edge(edge)


class TestGraphModification:
    def test_remove_node(self, simple_graph):
        graph, node1, node2, edge = simple_graph
        graph.remove_node(node1)
        assert node1.id not in graph.internal_nodes
        assert edge.id not in graph.internal_edges
        assert node1.id not in graph.node_edge_mapping

    def test_remove_edge(self, simple_graph):
        graph, node1, node2, edge = simple_graph
        graph.remove_edge(edge)
        assert edge.id not in graph.internal_edges
        assert edge.id not in graph.node_edge_mapping[node1.id]["out"]
        assert edge.id not in graph.node_edge_mapping[node2.id]["in"]

    def test_remove_nonexistent_node(self, simple_graph):
        graph, _, _, _ = simple_graph
        non_existent_node = create_test_node("NonExistent")
        with pytest.raises(RelationError):
            graph.remove_node(non_existent_node)

    def test_remove_nonexistent_edge(self, simple_graph):
        graph, node1, node2, _ = simple_graph
        non_existent_edge = Edge(head=node1, tail=node2)
        with pytest.raises(RelationError):
            graph.remove_edge(non_existent_edge)


class TestGraphContainment:
    def test_contains_node(self, simple_graph):
        graph, node1, node2, _ = simple_graph
        assert node1 in graph
        assert node2 in graph
        assert create_test_node("NonExistent") not in graph

    def test_contains_edge(self, simple_graph):
        graph, _, _, edge = simple_graph
        assert edge in graph
        assert Edge(head=create_test_node("Node1"), tail=create_test_node("Node2")) not in graph

    def test_empty_graph_contains(self, empty_graph):
        node = create_test_node("TestNode")
        edge = Edge(head=node, tail=node)
        assert node not in empty_graph
        assert edge not in empty_graph


class TestGraphProperties:
    def test_empty_graph_properties(self, empty_graph):
        assert len(empty_graph.internal_nodes) == 0
        assert len(empty_graph.internal_edges) == 0
        assert len(empty_graph.node_edge_mapping) == 0

    def test_single_node_graph(self):
        graph = Graph()
        node = create_test_node("SingleNode")
        graph.add_node(node)
        assert len(graph.internal_nodes) == 1
        assert len(graph.internal_edges) == 0
        assert node.id in graph.node_edge_mapping

    def test_self_loop(self):
        graph = Graph()
        node = create_test_node("SelfLoop")
        graph.add_node(node)
        edge = Edge(head=node, tail=node)
        graph.add_edge(edge)
        assert len(graph.internal_edges) == 1
        assert edge.id in graph.node_edge_mapping[node.id]["in"]
        assert edge.id in graph.node_edge_mapping[node.id]["out"]
