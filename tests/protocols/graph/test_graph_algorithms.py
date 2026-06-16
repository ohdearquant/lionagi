"""Tests for graph algorithms: get_tails, topological_sort, find_path."""

import pytest

from lionagi._errors import RelationError
from lionagi.protocols.graph.edge import EdgeCondition
from lionagi.protocols.graph.node import Node
from lionagi.protocols.types import Edge, Graph, Pile

from .helpers import create_test_node


@pytest.fixture
def linear_chain():
    """A -> B -> C -> D  (linear DAG)."""
    graph = Graph()
    nodes = [create_test_node(name) for name in ("A", "B", "C", "D")]
    for node in nodes:
        graph.add_node(node)

    edges = []
    for i in range(len(nodes) - 1):
        edge = Edge(head=nodes[i], tail=nodes[i + 1])
        graph.add_edge(edge)
        edges.append(edge)

    return graph, nodes, edges


@pytest.fixture
def diamond_dag():
    """Diamond-shaped DAG:

        A
       / \\
      B   C
       \\ /
        D
    """
    graph = Graph()
    a, b, c, d = (create_test_node(n) for n in ("A", "B", "C", "D"))
    for node in (a, b, c, d):
        graph.add_node(node)

    e_ab = Edge(head=a, tail=b)
    e_ac = Edge(head=a, tail=c)
    e_bd = Edge(head=b, tail=d)
    e_cd = Edge(head=c, tail=d)
    edges = [e_ab, e_ac, e_bd, e_cd]
    for edge in edges:
        graph.add_edge(edge)

    return graph, [a, b, c, d], edges


@pytest.fixture
def cyclic_graph():
    """A -> B -> C -> A  (cycle)."""
    graph = Graph()
    nodes = [create_test_node(name) for name in ("A", "B", "C")]
    for node in nodes:
        graph.add_node(node)

    edges = [
        Edge(head=nodes[0], tail=nodes[1]),
        Edge(head=nodes[1], tail=nodes[2]),
        Edge(head=nodes[2], tail=nodes[0]),
    ]
    for edge in edges:
        graph.add_edge(edge)

    return graph, nodes, edges


@pytest.fixture
def branching_dag():
    """DAG with multiple paths of different lengths:

    A -> B -> D -> F
    A -> C -> E -> F
    B -> E
    """
    graph = Graph()
    a, b, c, d, e, f = (create_test_node(n) for n in ("A", "B", "C", "D", "E", "F"))
    all_nodes = [a, b, c, d, e, f]
    for node in all_nodes:
        graph.add_node(node)

    e_ab = Edge(head=a, tail=b)
    e_ac = Edge(head=a, tail=c)
    e_bd = Edge(head=b, tail=d)
    e_be = Edge(head=b, tail=e)
    e_ce = Edge(head=c, tail=e)
    e_df = Edge(head=d, tail=f)
    e_ef = Edge(head=e, tail=f)
    edges = [e_ab, e_ac, e_bd, e_be, e_ce, e_df, e_ef]
    for edge in edges:
        graph.add_edge(edge)

    return graph, all_nodes, edges


# get_tails


class TestGetTails:
    """Tests for Graph.get_tails()."""

    def test_empty_graph_returns_empty(self):
        graph = Graph()
        tails = graph.get_tails()
        assert len(tails) == 0

    def test_single_node_no_edges_is_tail(self):
        graph = Graph()
        node = create_test_node("solo")
        graph.add_node(node)

        tails = graph.get_tails()
        assert len(tails) == 1
        assert node.id in tails

    def test_single_node_is_both_head_and_tail(self):
        graph = Graph()
        node = create_test_node("solo")
        graph.add_node(node)

        heads = graph.get_heads()
        tails = graph.get_tails()
        assert len(heads) == 1
        assert len(tails) == 1
        assert heads[0].id == tails[0].id == node.id

    def test_linear_chain_only_last_is_tail(self, linear_chain):
        graph, nodes, _ = linear_chain
        tails = graph.get_tails()
        assert len(tails) == 1
        assert nodes[-1].id in tails

    def test_diamond_dag_only_leaf_is_tail(self, diamond_dag):
        graph, nodes, _ = diamond_dag
        # Only D (index 3) should be a tail
        tails = graph.get_tails()
        assert len(tails) == 1
        assert nodes[3].id in tails

    def test_branching_dag_single_tail(self, branching_dag):
        graph, nodes, _ = branching_dag
        # Only F (index 5) is a tail
        tails = graph.get_tails()
        assert len(tails) == 1
        assert nodes[5].id in tails

    def test_cyclic_graph_no_tails(self, cyclic_graph):
        graph, _, _ = cyclic_graph
        tails = graph.get_tails()
        assert len(tails) == 0

    def test_multiple_tails(self):
        """Graph with two leaf nodes should return both as tails."""
        graph = Graph()
        a, b, c = (create_test_node(n) for n in ("A", "B", "C"))
        for node in (a, b, c):
            graph.add_node(node)

        graph.add_edge(Edge(head=a, tail=b))
        graph.add_edge(Edge(head=a, tail=c))

        tails = graph.get_tails()
        assert len(tails) == 2
        tail_ids = {node.id for node in tails}
        assert b.id in tail_ids
        assert c.id in tail_ids

    def test_tails_returns_pile(self, linear_chain):
        graph, _, _ = linear_chain
        tails = graph.get_tails()
        assert isinstance(tails, Pile)

    def test_disconnected_components_tails(self):
        """Each disconnected component contributes its own tail."""
        graph = Graph()
        a, b, c, d = (create_test_node(n) for n in ("A", "B", "C", "D"))
        for node in (a, b, c, d):
            graph.add_node(node)

        graph.add_edge(Edge(head=a, tail=b))
        graph.add_edge(Edge(head=c, tail=d))

        tails = graph.get_tails()
        assert len(tails) == 2
        tail_ids = {node.id for node in tails}
        assert b.id in tail_ids
        assert d.id in tail_ids


# topological_sort


class TestTopologicalSort:
    """Tests for Graph.topological_sort()."""

    def test_empty_graph_returns_empty(self):
        graph = Graph()
        result = graph.topological_sort()
        assert result == []

    def test_single_node(self):
        graph = Graph()
        node = create_test_node("only")
        graph.add_node(node)

        result = graph.topological_sort()
        assert len(result) == 1
        assert result[0].id == node.id

    def test_linear_chain_correct_order(self, linear_chain):
        graph, nodes, _ = linear_chain
        result = graph.topological_sort()

        assert len(result) == len(nodes)
        result_ids = [n.id for n in result]
        # A must come before B, B before C, C before D
        for i in range(len(nodes) - 1):
            assert result_ids.index(nodes[i].id) < result_ids.index(nodes[i + 1].id)

    def test_diamond_dag_valid_order(self, diamond_dag):
        graph, nodes, _ = diamond_dag
        a, b, c, d = nodes

        result = graph.topological_sort()
        assert len(result) == 4

        result_ids = [n.id for n in result]
        # A must come before B and C; B and C must come before D
        assert result_ids.index(a.id) < result_ids.index(b.id)
        assert result_ids.index(a.id) < result_ids.index(c.id)
        assert result_ids.index(b.id) < result_ids.index(d.id)
        assert result_ids.index(c.id) < result_ids.index(d.id)

    def test_branching_dag_valid_order(self, branching_dag):
        graph, nodes, _ = branching_dag
        a, b, c, d, e, f = nodes

        result = graph.topological_sort()
        assert len(result) == 6

        result_ids = [n.id for n in result]
        # A before B and C
        assert result_ids.index(a.id) < result_ids.index(b.id)
        assert result_ids.index(a.id) < result_ids.index(c.id)
        # B before D and E
        assert result_ids.index(b.id) < result_ids.index(d.id)
        assert result_ids.index(b.id) < result_ids.index(e.id)
        # C before E
        assert result_ids.index(c.id) < result_ids.index(e.id)
        # D and E before F
        assert result_ids.index(d.id) < result_ids.index(f.id)
        assert result_ids.index(e.id) < result_ids.index(f.id)

    def test_cyclic_graph_raises_value_error(self, cyclic_graph):
        graph, _, _ = cyclic_graph
        with pytest.raises(ValueError, match="cycles"):
            graph.topological_sort()

    def test_two_isolated_nodes(self):
        """Two unconnected nodes: both valid orderings are acceptable."""
        graph = Graph()
        x = create_test_node("X")
        y = create_test_node("Y")
        graph.add_node(x)
        graph.add_node(y)

        result = graph.topological_sort()
        assert len(result) == 2
        result_ids = {n.id for n in result}
        assert x.id in result_ids
        assert y.id in result_ids

    def test_topological_sort_returns_node_objects(self, linear_chain):
        graph, nodes, _ = linear_chain
        result = graph.topological_sort()
        for item in result:
            assert hasattr(item, "id")
            assert item.id in graph.internal_nodes


# find_path (async)


class TestFindPath:
    """Tests for Graph.find_path() (async BFS pathfinding)."""

    @pytest.mark.asyncio
    async def test_direct_edge_returns_single_edge(self, linear_chain):
        graph, nodes, edges = linear_chain
        # A -> B is a direct edge
        path = await graph.find_path(nodes[0], nodes[1])
        assert path is not None
        assert len(path) == 1
        assert path[0].head == nodes[0].id
        assert path[0].tail == nodes[1].id

    @pytest.mark.asyncio
    async def test_multi_hop_path_returns_edges_in_order(self, linear_chain):
        graph, nodes, _ = linear_chain
        # A -> B -> C -> D  (3 hops)
        path = await graph.find_path(nodes[0], nodes[3])
        assert path is not None
        assert len(path) == 3
        # Verify edge chain is continuous
        assert path[0].head == nodes[0].id
        assert path[0].tail == nodes[1].id
        assert path[1].head == nodes[1].id
        assert path[1].tail == nodes[2].id
        assert path[2].head == nodes[2].id
        assert path[2].tail == nodes[3].id

    @pytest.mark.asyncio
    async def test_no_path_returns_none(self):
        """Two disconnected nodes have no path between them."""
        graph = Graph()
        a = create_test_node("A")
        b = create_test_node("B")
        graph.add_node(a)
        graph.add_node(b)

        result = await graph.find_path(a, b)
        assert result is None

    @pytest.mark.asyncio
    async def test_same_start_and_end_returns_empty(self, linear_chain):
        graph, nodes, _ = linear_chain
        path = await graph.find_path(nodes[0], nodes[0])
        assert path is not None
        assert path == []

    @pytest.mark.asyncio
    async def test_start_not_in_graph_raises_relation_error(self):
        graph = Graph()
        a = create_test_node("A")
        b = create_test_node("B")
        graph.add_node(b)
        # a is NOT in graph

        with pytest.raises(RelationError, match="not found"):
            await graph.find_path(a, b)

    @pytest.mark.asyncio
    async def test_end_not_in_graph_raises_relation_error(self):
        graph = Graph()
        a = create_test_node("A")
        b = create_test_node("B")
        graph.add_node(a)
        # b is NOT in graph

        with pytest.raises(RelationError, match="not found"):
            await graph.find_path(a, b)

    @pytest.mark.asyncio
    async def test_both_not_in_graph_raises_relation_error(self):
        graph = Graph()
        a = create_test_node("A")
        b = create_test_node("B")

        with pytest.raises(RelationError, match="not found"):
            await graph.find_path(a, b)

    @pytest.mark.asyncio
    async def test_multiple_paths_returns_shortest(self, branching_dag):
        """BFS should find shortest path when multiple paths exist."""
        graph, nodes, _ = branching_dag
        a, b, c, d, e, f = nodes

        # Paths from A to F:
        #   A -> B -> D -> F  (3 hops)
        #   A -> B -> E -> F  (3 hops)
        #   A -> C -> E -> F  (3 hops)
        # BFS finds one of the shortest (all are length 3 here)
        path = await graph.find_path(a, f)
        assert path is not None
        assert len(path) == 3

        # Verify it is a valid path: first edge starts at A, last ends at F
        assert path[0].head == a.id
        assert path[-1].tail == f.id

        # Verify edge chain is continuous
        for i in range(len(path) - 1):
            assert path[i].tail == path[i + 1].head

    @pytest.mark.asyncio
    async def test_bfs_prefers_shorter_path(self):
        """When one path is shorter, BFS returns it."""
        graph = Graph()
        a, b, c, d = (create_test_node(n) for n in ("A", "B", "C", "D"))
        for node in (a, b, c, d):
            graph.add_node(node)

        # Long path: A -> B -> C -> D
        graph.add_edge(Edge(head=a, tail=b))
        graph.add_edge(Edge(head=b, tail=c))
        graph.add_edge(Edge(head=c, tail=d))
        # Short path: A -> D
        graph.add_edge(Edge(head=a, tail=d))

        path = await graph.find_path(a, d)
        assert path is not None
        assert len(path) == 1  # Direct edge A -> D
        assert path[0].head == a.id
        assert path[0].tail == d.id

    @pytest.mark.asyncio
    async def test_reverse_direction_no_path(self, linear_chain):
        """Path in reverse direction should not exist in directed graph."""
        graph, nodes, _ = linear_chain
        # D -> A has no path (edges go A -> B -> C -> D)
        result = await graph.find_path(nodes[3], nodes[0])
        assert result is None

    @pytest.mark.asyncio
    async def test_path_returns_edge_objects(self, linear_chain):
        graph, nodes, _ = linear_chain
        path = await graph.find_path(nodes[0], nodes[1])
        assert path is not None
        for item in path:
            assert isinstance(item, Edge)

    @pytest.mark.asyncio
    async def test_cyclic_graph_find_path(self, cyclic_graph):
        """BFS should still find a path in a cyclic graph."""
        graph, nodes, _ = cyclic_graph
        # A -> B exists directly
        path = await graph.find_path(nodes[0], nodes[1])
        assert path is not None
        assert len(path) == 1

        # A -> C goes A -> B -> C
        path = await graph.find_path(nodes[0], nodes[2])
        assert path is not None
        assert len(path) == 2

    @pytest.mark.asyncio
    async def test_find_path_accepts_node_ids(self, linear_chain):
        """find_path should accept node IDs (UUIDs) as well as node objects."""
        graph, nodes, _ = linear_chain
        path = await graph.find_path(nodes[0].id, nodes[1].id)
        assert path is not None
        assert len(path) == 1


class _FalseCondition(EdgeCondition):
    """Condition that always returns False (blocks traversal)."""

    async def apply(self, *args, **kwargs) -> bool:
        return False


@pytest.mark.asyncio
async def test_graph_find_path_skips_edges_when_condition_false():
    """With check_conditions=True, edges whose condition returns False are skipped."""
    g = Graph()
    a, b, c = Node(), Node(), Node()
    for n in (a, b, c):
        g.add_node(n)

    # A --(blocking condition)--> B --> C
    e_ab = Edge(head=a.id, tail=b.id, condition=_FalseCondition())
    e_bc = Edge(head=b.id, tail=c.id)
    g.add_edge(e_ab)
    g.add_edge(e_bc)

    # With conditions checked, A->B is blocked so no path A->C exists
    result_blocked = await g.find_path(a, c, check_conditions=True)
    assert result_blocked is None

    # Without condition checking, path A->B->C is found
    result_free = await g.find_path(a, c, check_conditions=False)
    assert result_free is not None
    assert len(result_free) == 2
