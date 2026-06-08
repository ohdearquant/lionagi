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

    # Create nodes
    node1 = Node()
    node2 = Node()

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
    nodes = [Node() for _ in range(4)]

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


@pytest.fixture
def cyclic_graph():
    """Fixture for cyclic graph"""
    graph = Graph()

    # Create nodes
    nodes = [Node() for _ in range(3)]

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

    return graph, nodes, edges


class TestGraphBasics:
    """Test basic graph operations"""

    def test_empty_graph_creation(self, empty_graph):
        """Test creation of empty graph"""
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
        """Test adding invalid node type"""
        with pytest.raises(RelationError):
            empty_graph.add_node("not a node")

    def test_add_duplicate_node(self, empty_graph):
        """Test adding duplicate node"""
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
        """Test adding invalid edge"""
        with pytest.raises(RelationError):
            empty_graph.add_edge("not an edge")

    def test_add_edge_missing_nodes(self, empty_graph):
        """Test adding edge with missing nodes"""
        node1 = Node()
        node2 = Node()
        edge = Edge(head=node1, tail=node2)
        with pytest.raises(RelationError):
            empty_graph.add_edge(edge)


class TestGraphTraversal:
    """Test graph traversal operations"""

    def test_get_heads(self, complex_graph):
        """Test getting head nodes"""
        graph, nodes, _ = complex_graph
        heads = graph.get_heads()
        assert len(heads) == 1
        assert nodes[0].id in heads

    def test_get_predecessors(self, complex_graph):
        """Test getting predecessor nodes"""
        graph, nodes, _ = complex_graph
        predecessors = graph.get_predecessors(nodes[3])
        assert len(predecessors) == 2
        pred_ids = {node.id for node in predecessors}
        assert nodes[0].id in pred_ids
        assert nodes[2].id in pred_ids

    def test_get_successors(self, complex_graph):
        """Test getting successor nodes"""
        graph, nodes, _ = complex_graph
        successors = graph.get_successors(nodes[0])
        assert len(successors) == 2
        succ_ids = {node.id for node in successors}
        assert nodes[1].id in succ_ids
        assert nodes[3].id in succ_ids

    def test_find_node_edge(self, complex_graph):
        """Test finding edges connected to a node"""
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
        """Test removing a node"""
        graph, node1, node2, edge = simple_graph
        graph.remove_node(node1)
        assert node1.id not in graph.internal_nodes
        assert edge.id not in graph.internal_edges
        assert node1.id not in graph.node_edge_mapping

    def test_remove_edge(self, simple_graph):
        """Test removing an edge"""
        graph, node1, node2, edge = simple_graph
        graph.remove_edge(edge)
        assert edge.id not in graph.internal_edges
        assert edge.id not in graph.node_edge_mapping[node1.id]["out"]
        assert edge.id not in graph.node_edge_mapping[node2.id]["in"]


class TestGraphProperties:
    """Test graph property checks"""

    def test_is_acyclic_true(self, complex_graph):
        """Test acyclic graph detection"""
        graph, _, _ = complex_graph
        assert graph.is_acyclic()

    def test_is_acyclic_false(self, cyclic_graph):
        """Test cyclic graph detection"""
        graph, _, _ = cyclic_graph
        assert not graph.is_acyclic()


class TestEdgeConditions:
    """Test edge conditions"""

    @pytest.mark.asyncio
    async def test_edge_condition_true(self):
        """Test edge condition that evaluates to True"""

        class TrueCondition(EdgeCondition):
            async def apply(self, *args, **kwargs):
                return True

        edge = Edge(head=Node(), tail=Node(), condition=TrueCondition())
        assert await edge.check_condition()

    @pytest.mark.asyncio
    async def test_edge_condition_false(self):
        """Test edge condition that evaluates to False"""

        class FalseCondition(EdgeCondition):
            async def apply(self, *args, **kwargs):
                return False

        edge = Edge(head=Node(), tail=Node(), condition=FalseCondition())
        assert not await edge.check_condition()

    @pytest.mark.asyncio
    async def test_edge_no_condition(self):
        """Test edge with no condition"""
        edge = Edge(head=Node(), tail=Node())
        assert await edge.check_condition()


class TestGraphContainment:
    """Test graph containment operations"""

    def test_contains_node(self, simple_graph):
        """Test node containment check"""
        graph, node1, node2, _ = simple_graph
        assert node1 in graph
        assert node2 in graph
        assert Node() not in graph

    def test_contains_edge(self, simple_graph):
        """Test edge containment check"""
        graph, _, _, edge = simple_graph
        assert edge in graph
        assert Edge(head=Node(), tail=Node()) not in graph


# ---------------------------------------------------------------------------
# Edge case: Graph with cycles -- add_edge creating a cycle, is_acyclic detects it
# ---------------------------------------------------------------------------


class TestGraphCycleDetection:
    def test_add_cycle_edge_and_detect(self):
        graph = Graph()
        n1, n2, n3 = Node(), Node(), Node()
        for n in (n1, n2, n3):
            graph.add_node(n)
        graph.add_edge(Edge(head=n1, tail=n2))
        graph.add_edge(Edge(head=n2, tail=n3))
        graph.add_edge(Edge(head=n3, tail=n1))
        assert not graph.is_acyclic()

    def test_self_loop_is_cycle(self):
        graph = Graph()
        n = Node()
        graph.add_node(n)
        graph.add_edge(Edge(head=n, tail=n))
        assert not graph.is_acyclic()


# ---------------------------------------------------------------------------
# Edge case: Graph.remove_node with many edges (correctness)
# ---------------------------------------------------------------------------


class TestGraphRemoveNodeManyEdges:
    def test_remove_hub_node_cleans_all_edges(self):
        n = 50
        graph = Graph()
        hub = Node(content="hub")
        graph.add_node(hub)
        spokes = [Node(content=f"spoke-{i}") for i in range(n)]
        for spoke in spokes:
            graph.add_node(spoke)
            graph.add_edge(Edge(head=hub, tail=spoke))
            graph.add_edge(Edge(head=spoke, tail=hub))

        assert len(graph.internal_edges) == 2 * n
        graph.remove_node(hub)
        assert hub.id not in graph.internal_nodes
        assert hub.id not in graph.node_edge_mapping
        assert len(graph.internal_edges) == 0

        for spoke in spokes:
            assert len(graph.node_edge_mapping[spoke.id]["in"]) == 0
            assert len(graph.node_edge_mapping[spoke.id]["out"]) == 0


# ---------------------------------------------------------------------------
# Edge case: Graph serialization round-trip (to_dict / from_dict)
# ---------------------------------------------------------------------------


class TestGraphSerialization:
    def test_to_dict_produces_serializable_dict(self):
        g = Graph()
        n1, n2 = Node(content="a"), Node(content="b")
        g.add_node(n1)
        g.add_node(n2)
        e = Edge(head=n1, tail=n2)
        g.add_edge(e)
        d = g.to_dict()
        assert isinstance(d, dict)
        assert "internal_nodes" in d
        assert "internal_edges" in d

    def test_round_trip_via_model_dump_raises_known_validation_error(self):
        # Graph.from_dict uses model_validate which cannot reconstruct Pile fields
        # due to Metadata class mismatch -- this is a known limitation.
        g = Graph()
        d = g.model_dump()
        with pytest.raises(Exception):
            Graph.model_validate(d)


# ---------------------------------------------------------------------------
# Edge case: Graph.replace_node preserves edge connectivity
# ---------------------------------------------------------------------------


class TestGraphReplaceNode:
    def test_replace_node_rewires_edges(self):
        g = Graph()
        n1, n2, n3 = Node(content=1), Node(content=2), Node(content=3)
        for n in (n1, n2, n3):
            g.add_node(n)
        e1 = Edge(head=n1, tail=n2)
        e2 = Edge(head=n2, tail=n3)
        g.add_edge(e1)
        g.add_edge(e2)

        replacement = Node(content=99)
        g.replace_node(n2, replacement)

        assert n2.id not in g.internal_nodes
        assert replacement.id in g.internal_nodes
        assert g.node_edge_mapping[n1.id]["out"][e1.id] == replacement.id
        assert g.node_edge_mapping[n3.id]["in"][e2.id] == replacement.id

    def test_replace_node_with_existing_uuid_raises(self):
        from lionagi._errors import RelationError

        g = Graph()
        n1, n2 = Node(), Node()
        g.add_node(n1)
        g.add_node(n2)
        with pytest.raises(RelationError):
            g.replace_node(n1, n2)


# ---------------------------------------------------------------------------
# Edge case: Edge condition evaluation during find_path
# ---------------------------------------------------------------------------


class TestGraphEdgeConditionDuringTraversal:
    @pytest.mark.asyncio
    async def test_find_path_skips_blocked_edge_when_check_conditions(self):
        from lionagi.protocols.types import EdgeCondition

        class BlockCondition(EdgeCondition):
            async def apply(self, *args, **kwargs):
                return False

        g = Graph()
        n1, n2, n3 = Node(content=1), Node(content=2), Node(content=3)
        for n in (n1, n2, n3):
            g.add_node(n)
        # Direct blocked path: n1 -> n2
        blocked = Edge(head=n1, tail=n2, condition=BlockCondition())
        g.add_edge(blocked)
        # Alternative path: n1 -> n3 -> n2
        e2 = Edge(head=n1, tail=n3)
        e3 = Edge(head=n3, tail=n2)
        g.add_edge(e2)
        g.add_edge(e3)

        path = await g.find_path(n1, n2, check_conditions=True)
        assert path is not None
        edge_ids = [e.id for e in path]
        assert blocked.id not in edge_ids

    @pytest.mark.asyncio
    async def test_find_path_no_path_when_all_blocked(self):
        from lionagi.protocols.types import EdgeCondition

        class BlockCondition(EdgeCondition):
            async def apply(self, *args, **kwargs):
                return False

        g = Graph()
        n1, n2 = Node(), Node()
        g.add_node(n1)
        g.add_node(n2)
        blocked = Edge(head=n1, tail=n2, condition=BlockCondition())
        g.add_edge(blocked)

        path = await g.find_path(n1, n2, check_conditions=True)
        assert path is None
