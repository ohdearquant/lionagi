import pytest

from lionagi.protocols.types import Edge, Graph, Pile

from .helpers import create_test_node


@pytest.fixture
def traversal_graph():
    """Fixture for graph with multiple paths for traversal testing"""
    graph = Graph()

    # Create nodes
    nodes = [create_test_node(f"Node{i}") for i in range(6)]

    # Add nodes
    for node in nodes:
        graph.add_node(node)

    # Create edges forming multiple paths
    edges = [
        Edge(head=nodes[0], tail=nodes[1]),  # 0 -> 1
        Edge(head=nodes[0], tail=nodes[2]),  # 0 -> 2
        Edge(head=nodes[1], tail=nodes[3]),  # 1 -> 3
        Edge(head=nodes[2], tail=nodes[3]),  # 2 -> 3
        Edge(head=nodes[3], tail=nodes[4]),  # 3 -> 4
        Edge(head=nodes[3], tail=nodes[5]),  # 3 -> 5
    ]

    # Add edges
    for edge in edges:
        graph.add_edge(edge)
        node = graph.internal_nodes[edge.head]
        node.add_relation(edge, "out")
        node = graph.internal_nodes[edge.tail]
        node.add_relation(edge, "in")

    return graph, nodes, edges


@pytest.fixture
def cyclic_graph():
    """Fixture for cyclic graph"""
    graph = Graph()

    # Create nodes
    nodes = [create_test_node(f"Node{i}") for i in range(3)]

    # Add nodes
    for node in nodes:
        graph.add_node(node)

    # Create edges forming a cycle
    edges = [
        Edge(head=nodes[0], tail=nodes[1]),  # 0 -> 1
        Edge(head=nodes[1], tail=nodes[2]),  # 1 -> 2
        Edge(head=nodes[2], tail=nodes[0]),  # 2 -> 0 (creates cycle)
    ]

    # Add edges
    for edge in edges:
        graph.add_edge(edge)
        node = graph.internal_nodes[edge.head]
        node.add_relation(edge, "out")
        node = graph.internal_nodes[edge.tail]
        node.add_relation(edge, "in")

    return graph, nodes, edges


class TestGraphTraversal:
    def test_get_heads(self, traversal_graph):
        graph, nodes, _ = traversal_graph
        heads = graph.get_heads()
        assert len(heads) == 1
        assert nodes[0].id in heads

    def test_get_predecessors(self, traversal_graph):
        graph, nodes, _ = traversal_graph
        # Node 3 has two predecessors (1 and 2)
        predecessors = graph.get_predecessors(nodes[3])
        assert len(predecessors) == 2
        pred_ids = {node.id for node in predecessors}
        assert nodes[1].id in pred_ids
        assert nodes[2].id in pred_ids

    def test_get_successors(self, traversal_graph):
        graph, nodes, _ = traversal_graph
        # Node 3 has two successors (4 and 5)
        successors = graph.get_successors(nodes[3])
        assert len(successors) == 2
        succ_ids = {node.id for node in successors}
        assert nodes[4].id in succ_ids
        assert nodes[5].id in succ_ids

    def test_find_node_edge_both(self, traversal_graph):
        graph, nodes, _ = traversal_graph
        # Node 3 has 2 incoming and 2 outgoing edges
        edges = graph.find_node_edge(nodes[3], direction="both")
        assert len(edges) == 4

    def test_find_node_edge_in(self, traversal_graph):
        graph, nodes, _ = traversal_graph
        # Node 3 has 2 incoming edges
        edges = graph.find_node_edge(nodes[3], direction="in")
        assert len(edges) == 2
        for edge in edges:
            assert edge.tail == nodes[3].id

    def test_find_node_edge_out(self, traversal_graph):
        graph, nodes, _ = traversal_graph
        # Node 3 has 2 outgoing edges
        edges = graph.find_node_edge(nodes[3], direction="out")
        assert len(edges) == 2
        for edge in edges:
            assert edge.head == nodes[3].id

    def test_find_node_edge_invalid_direction(self, traversal_graph):
        graph, nodes, _ = traversal_graph
        with pytest.raises(ValueError):
            graph.find_node_edge(nodes[0], direction="invalid")


class TestGraphCyclicProperties:
    def test_cyclic_graph_heads(self, cyclic_graph):
        graph, _, _ = cyclic_graph
        heads = graph.get_heads()
        assert len(heads) == 0  # No heads in a cycle

    def test_cyclic_graph_predecessors(self, cyclic_graph):
        graph, nodes, _ = cyclic_graph
        for node in nodes:
            predecessors = graph.get_predecessors(node)
            assert len(predecessors) == 1  # Each node has one predecessor

    def test_cyclic_graph_successors(self, cyclic_graph):
        graph, nodes, _ = cyclic_graph
        for node in nodes:
            successors = graph.get_successors(node)
            assert len(successors) == 1  # Each node has one successor

    def test_is_acyclic(self, traversal_graph, cyclic_graph):
        acyclic_graph, _, _ = traversal_graph
        cyclic, _, _ = cyclic_graph

        assert acyclic_graph.is_acyclic()  # Should be True
        assert not cyclic.is_acyclic()  # Should be False


class TestGraphTraversalEdgeCases:
    def test_isolated_node_traversal(self):
        graph = Graph()
        node = create_test_node("Isolated")
        graph.add_node(node)

        assert len(graph.get_predecessors(node)) == 0
        assert len(graph.get_successors(node)) == 0
        assert len(graph.find_node_edge(node)) == 0

    def test_disconnected_components_traversal(self):
        graph = Graph()

        # Create two disconnected components
        node1 = create_test_node("Component1_1")
        node2 = create_test_node("Component1_2")
        node3 = create_test_node("Component2_1")
        node4 = create_test_node("Component2_2")

        for node in [node1, node2, node3, node4]:
            graph.add_node(node)

        # Add edges within components
        edge1 = Edge(head=node1, tail=node2)
        edge2 = Edge(head=node3, tail=node4)

        for edge in [edge1, edge2]:
            graph.add_edge(edge)
            node = graph.internal_nodes[edge.head]
            node.add_relation(edge, "out")
            node = graph.internal_nodes[edge.tail]
            node.add_relation(edge, "in")

        # Both node1 and node3 should be heads
        heads = graph.get_heads()
        assert len(heads) == 2
        head_ids = {node.id for node in heads}
        assert node1.id in head_ids
        assert node3.id in head_ids

    def test_bidirectional_edge_traversal(self):
        graph = Graph()
        node1 = create_test_node("BiNode1")
        node2 = create_test_node("BiNode2")

        graph.add_node(node1)
        graph.add_node(node2)

        # Add edges in both directions
        edge1 = Edge(head=node1, tail=node2)
        edge2 = Edge(head=node2, tail=node1)

        for edge in [edge1, edge2]:
            graph.add_edge(edge)
            node = graph.internal_nodes[edge.head]
            node.add_relation(edge, "out")
            node = graph.internal_nodes[edge.tail]
            node.add_relation(edge, "in")

        # Both nodes should have one predecessor and one successor
        assert len(graph.get_predecessors(node1)) == 1
        assert len(graph.get_successors(node1)) == 1
        assert len(graph.get_predecessors(node2)) == 1
        assert len(graph.get_successors(node2)) == 1

    def test_multi_edge_traversal(self):
        graph = Graph()
        node1 = create_test_node("MultiNode1")
        node2 = create_test_node("MultiNode2")

        graph.add_node(node1)
        graph.add_node(node2)

        # Add multiple edges between same nodes
        edge1 = Edge(head=node1, tail=node2)
        edge2 = Edge(head=node1, tail=node2)

        for edge in [edge1, edge2]:
            graph.add_edge(edge)
            node = graph.internal_nodes[edge.head]
            node.add_relation(edge, "out")
            node = graph.internal_nodes[edge.tail]
            node.add_relation(edge, "in")

        # Should find both edges
        edges = graph.find_node_edge(node1, direction="out")
        assert len(edges) == 2
        assert isinstance(edges, Pile)
        assert all(edge.head == node1.id and edge.tail == node2.id for edge in edges)
