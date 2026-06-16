import pytest

from lionagi._errors import RelationError
from lionagi.protocols.types import Edge, EdgeCondition, Graph, Node


@pytest.fixture
def empty_graph():
    """Fixture for empty graph"""
    return Graph()


@pytest.fixture
def simple_graph():
    """Fixture for simple graph with two connected nodes"""
    graph = Graph()

    node1 = Node()
    node2 = Node()

    graph.add_node(node1)
    graph.add_node(node2)

    edge = Edge(head=node1, tail=node2)
    graph.add_edge(edge)

    return graph, node1, node2, edge


@pytest.fixture
def complex_graph():
    """Fixture for complex graph with multiple nodes and edges"""
    graph = Graph()

    nodes = [Node() for _ in range(4)]

    for node in nodes:
        graph.add_node(node)

    edges = [
        Edge(head=nodes[0], tail=nodes[1]),  # 0 -> 1
        Edge(head=nodes[1], tail=nodes[2]),  # 1 -> 2
        Edge(head=nodes[2], tail=nodes[3]),  # 2 -> 3
        Edge(head=nodes[0], tail=nodes[3]),  # 0 -> 3
    ]

    for edge in edges:
        graph.add_edge(edge)

    return graph, nodes, edges


@pytest.fixture
def cyclic_graph():
    """Fixture for cyclic graph"""
    graph = Graph()

    nodes = [Node() for _ in range(3)]

    for node in nodes:
        graph.add_node(node)

    edges = [
        Edge(head=nodes[0], tail=nodes[1]),  # 0 -> 1
        Edge(head=nodes[1], tail=nodes[2]),  # 1 -> 2
        Edge(head=nodes[2], tail=nodes[0]),  # 2 -> 0 (creates cycle)
    ]

    for edge in edges:
        graph.add_edge(edge)

    return graph, nodes, edges


class TestGraphBasics:
    """Test basic graph operations"""

    def test_empty_graph_creation(self, empty_graph):
        assert len(empty_graph.internal_nodes) == 0
        assert len(empty_graph.internal_edges) == 0
        assert isinstance(empty_graph.node_edge_mapping, dict)

    def test_add_node(self, empty_graph):
        """Adding a node increments internal_nodes and initialises edge mapping."""
        node = Node()
        count_before = len(empty_graph.internal_nodes)
        empty_graph.add_node(node)
        assert len(empty_graph.internal_nodes) == count_before + 1
        assert node.id in empty_graph.internal_nodes
        assert empty_graph.node_edge_mapping[node.id] == {"in": {}, "out": {}}

    def test_add_multiple_nodes_all_present(self, empty_graph):
        """Multiple distinct nodes all appear in internal_nodes."""
        nodes = [Node() for _ in range(3)]
        for n in nodes:
            empty_graph.add_node(n)
        for n in nodes:
            assert n.id in empty_graph.internal_nodes

    def test_add_invalid_node(self, empty_graph):
        with pytest.raises(RelationError):
            empty_graph.add_node("not a node")

    def test_add_duplicate_node(self, empty_graph):
        node = Node()
        empty_graph.add_node(node)
        with pytest.raises(RelationError):
            empty_graph.add_node(node)

    def test_add_edge(self, simple_graph):
        """Adding an edge updates both head (out) and tail (in) node mappings."""
        graph, node1, node2, edge = simple_graph
        assert edge.id in graph.internal_edges
        assert graph.node_edge_mapping[node1.id]["out"][edge.id] == node2.id
        assert graph.node_edge_mapping[node2.id]["in"][edge.id] == node1.id
        # head node has no in-edges from this edge
        assert edge.id not in graph.node_edge_mapping[node1.id]["in"]
        # tail node has no out-edges from this edge
        assert edge.id not in graph.node_edge_mapping[node2.id]["out"]

    def test_add_invalid_edge(self, empty_graph):
        with pytest.raises(RelationError):
            empty_graph.add_edge("not an edge")

    def test_add_edge_missing_nodes(self, empty_graph):
        node1 = Node()
        node2 = Node()
        edge = Edge(head=node1, tail=node2)
        with pytest.raises(RelationError):
            empty_graph.add_edge(edge)


class TestGraphTraversal:
    """Test graph traversal operations"""

    def test_get_heads(self, complex_graph):
        graph, nodes, _ = complex_graph
        heads = graph.get_heads()
        assert len(heads) == 1
        assert nodes[0].id in heads

    def test_get_predecessors(self, complex_graph):
        graph, nodes, _ = complex_graph
        predecessors = graph.get_predecessors(nodes[3])
        assert len(predecessors) == 2
        pred_ids = {node.id for node in predecessors}
        assert nodes[0].id in pred_ids
        assert nodes[2].id in pred_ids

    def test_get_successors(self, complex_graph):
        graph, nodes, _ = complex_graph
        successors = graph.get_successors(nodes[0])
        assert len(successors) == 2
        succ_ids = {node.id for node in successors}
        assert nodes[1].id in succ_ids
        assert nodes[3].id in succ_ids

    def test_find_node_edge(self, complex_graph):
        graph, nodes, edges = complex_graph

        # Test outgoing edges
        out_edges = graph.find_node_edge(nodes[0], direction="out")
        assert len(out_edges) == 2

        # Test incoming edges
        in_edges = graph.find_node_edge(nodes[3], direction="in")
        assert len(in_edges) == 2

        # Test both directions
        both_edges = graph.find_node_edge(nodes[1], direction="both")
        assert len(both_edges) == 2


class TestGraphModification:
    """Test graph modification operations"""

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


class TestGraphProperties:
    """Test graph property checks"""

    def test_is_acyclic_true(self, complex_graph):
        graph, _, _ = complex_graph
        assert graph.is_acyclic()

    def test_is_acyclic_false(self, cyclic_graph):
        graph, _, _ = cyclic_graph
        assert not graph.is_acyclic()


class TestEdgeConditions:
    """Test edge conditions"""

    @pytest.mark.asyncio
    async def test_edge_condition_true(self):
        class TrueCondition(EdgeCondition):
            async def apply(self, *args, **kwargs):
                return True

        edge = Edge(head=Node(), tail=Node(), condition=TrueCondition())
        assert await edge.check_condition()

    @pytest.mark.asyncio
    async def test_edge_condition_false(self):
        class FalseCondition(EdgeCondition):
            async def apply(self, *args, **kwargs):
                return False

        edge = Edge(head=Node(), tail=Node(), condition=FalseCondition())
        assert not await edge.check_condition()

    @pytest.mark.asyncio
    async def test_edge_no_condition(self):
        edge = Edge(head=Node(), tail=Node())
        assert await edge.check_condition()


class TestGraphContainment:
    """Test graph containment operations"""

    def test_contains_node(self, simple_graph):
        graph, node1, node2, _ = simple_graph
        assert node1 in graph
        assert node2 in graph
        assert Node() not in graph

    def test_contains_edge(self, simple_graph):
        graph, _, _, edge = simple_graph
        assert edge in graph
        assert Edge(head=Node(), tail=Node()) not in graph
