import pytest

from lionagi.protocols.types import Edge, Graph

from .helpers import GraphNode


class TypeANode(GraphNode):
    value: int


class TypeBNode(GraphNode):
    name: str


class WeightedEdge(Edge):
    @property
    def weight(self) -> float:
        return self.properties.get("weight")


class LabeledEdge(Edge):
    @property
    def labels(self) -> list[str]:
        return self.properties.get("labels")


@pytest.fixture
def mixed_type_graph():
    """Fixture for graph with mixed node and edge types"""
    graph = Graph()

    # Create different types of nodes
    node_a1 = TypeANode(value=10)
    node_a2 = TypeANode(value=20)
    node_b1 = TypeBNode(name="B1")
    node_b2 = TypeBNode(name="B2")

    # Add nodes
    for node in [node_a1, node_a2, node_b1, node_b2]:
        graph.add_node(node)

    # Create different types of edges
    edge1 = WeightedEdge(head=node_a1, tail=node_b1, weight=5.0)
    edge2 = LabeledEdge(head=node_a2, tail=node_b2, labels=["test", "connection"])

    # Add edges
    for edge in [edge1, edge2]:
        graph.add_edge(edge)
        node = graph.internal_nodes[edge.head]
        node.add_relation(edge, "out")
        node = graph.internal_nodes[edge.tail]
        node.add_relation(edge, "in")

    return (
        graph,
        {
            "node_a1": node_a1,
            "node_a2": node_a2,
            "node_b1": node_b1,
            "node_b2": node_b2,
        },
        {"edge1": edge1, "edge2": edge2},
    )


class TestNodeTypes:
    def test_type_a_nodes(self, mixed_type_graph):
        graph, nodes, _ = mixed_type_graph

        # Verify TypeA nodes
        node_a1 = nodes["node_a1"]
        node_a2 = nodes["node_a2"]

        assert isinstance(node_a1, TypeANode)
        assert isinstance(node_a2, TypeANode)
        assert node_a1.value == 10
        assert node_a2.value == 20

    def test_type_b_nodes(self, mixed_type_graph):
        graph, nodes, _ = mixed_type_graph

        # Verify TypeB nodes
        node_b1 = nodes["node_b1"]
        node_b2 = nodes["node_b2"]

        assert isinstance(node_b1, TypeBNode)
        assert isinstance(node_b2, TypeBNode)
        assert node_b1.name == "B1"
        assert node_b2.name == "B2"

    def test_node_type_fields(self, mixed_type_graph):
        graph, nodes, _ = mixed_type_graph

        # Test TypeA specific field
        node_a1 = nodes["node_a1"]
        node_a1.value = 30
        assert graph.internal_nodes[node_a1.id].value == 30

        # Test TypeB specific field
        node_b1 = nodes["node_b1"]
        node_b1.name = "NewB1"
        assert graph.internal_nodes[node_b1.id].name == "NewB1"


class TestEdgeTypes:
    def test_weighted_edges(self, mixed_type_graph):
        graph, _, edges = mixed_type_graph
        edge1 = edges["edge1"]

        assert isinstance(edge1, WeightedEdge)
        assert edge1.weight == 5.0

        # Test weight modification
        edge1.update_property("weight", 7.5)
        assert graph.internal_edges[edge1.id].weight == 7.5

    def test_labeled_edges(self, mixed_type_graph):
        graph, _, edges = mixed_type_graph
        edge2 = edges["edge2"]

        assert isinstance(edge2, LabeledEdge)
        assert "test" in edge2.labels
        assert "connection" in edge2.labels

        # Test labels modification
        new_labels = edge2.labels + ["new_label"]
        edge2.update_property("labels", new_labels)
        assert "new_label" in graph.internal_edges[edge2.id].labels


class TestMixedTypeOperations:
    def test_mixed_type_traversal(self, mixed_type_graph):
        graph, nodes, _ = mixed_type_graph

        # Get successors of TypeA nodes
        node_a1 = nodes["node_a1"]
        successors = graph.get_successors(node_a1)
        assert len(successors) == 1
        assert isinstance(successors[0], TypeBNode)

        # Get predecessors of TypeB nodes
        node_b1 = nodes["node_b1"]
        predecessors = graph.get_predecessors(node_b1)
        assert len(predecessors) == 1
        assert isinstance(predecessors[0], TypeANode)

    def test_mixed_edge_finding(self, mixed_type_graph):
        graph, nodes, _ = mixed_type_graph

        # Find edges from node_a1
        node_a1 = nodes["node_a1"]
        edges = graph.find_node_edge(node_a1, direction="out")
        assert len(edges) == 1
        assert isinstance(edges[0], WeightedEdge)

        # Find edges from node_a2
        node_a2 = nodes["node_a2"]
        edges = graph.find_node_edge(node_a2, direction="out")
        assert len(edges) == 1
        assert isinstance(edges[0], LabeledEdge)
