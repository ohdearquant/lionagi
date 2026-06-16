import asyncio
import random

import pytest

from lionagi._errors import RelationError
from lionagi.protocols.types import Edge, Graph

from .helpers import create_test_node


@pytest.mark.slow
class TestGraphPerformance:
    """Test graph performance with large datasets"""

    def test_large_graph_creation(self):
        graph = Graph()
        num_nodes = 1000
        num_edges = 5000

        # Add nodes
        nodes = [create_test_node(f"Node{i}") for i in range(num_nodes)]
        for node in nodes:
            graph.add_node(node)

        # Add random edges
        edge_count = 0
        for _ in range(num_edges):
            head = random.choice(nodes)
            tail = random.choice(nodes)
            if head != tail:
                try:
                    edge = Edge(head=head, tail=tail)
                    graph.add_edge(edge)
                    head_node = graph.internal_nodes[edge.head]
                    tail_node = graph.internal_nodes[edge.tail]
                    head_node.add_relation(edge, "out")
                    tail_node.add_relation(edge, "in")
                    edge_count += 1
                except RelationError:
                    pass  # Ignore duplicate edges

        assert len(graph.internal_nodes) == num_nodes
        assert len(graph.internal_edges) <= num_edges
        assert edge_count > 0

    def test_large_graph_operations(self):
        graph = Graph()
        num_nodes = 1000

        # Create a chain of nodes
        nodes = [create_test_node(f"Node{i}") for i in range(num_nodes)]
        for node in nodes:
            graph.add_node(node)

        # Connect nodes in sequence
        for i in range(num_nodes - 1):
            edge = Edge(head=nodes[i], tail=nodes[i + 1])
            graph.add_edge(edge)
            head_node = graph.internal_nodes[edge.head]
            tail_node = graph.internal_nodes[edge.tail]
            head_node.add_relation(edge, "out")
            tail_node.add_relation(edge, "in")

        # Test traversal performance
        start_node = nodes[0]
        end_node = nodes[-1]

        # Traverse from start to end
        current = start_node
        path_length = 0
        while current != end_node:
            successors = graph.get_successors(current)
            assert len(successors) == 1
            current = successors[0]
            path_length += 1

        assert path_length == num_nodes - 1

    def test_bulk_operations(self):
        graph = Graph()
        num_operations = 1000

        # Bulk node additions
        nodes = [create_test_node(f"Node{i}") for i in range(num_operations)]
        for node in nodes:
            graph.add_node(node)

        assert len(graph.internal_nodes) == num_operations

        # Bulk edge additions
        edge_count = 0
        for i in range(num_operations - 1):
            try:
                edge = Edge(head=nodes[i], tail=nodes[i + 1])
                graph.add_edge(edge)
                head_node = graph.internal_nodes[edge.head]
                tail_node = graph.internal_nodes[edge.tail]
                head_node.add_relation(edge, "out")
                tail_node.add_relation(edge, "in")
                edge_count += 1
            except RelationError:
                pass

        assert len(graph.internal_edges) == edge_count

        # Bulk node removals
        nodes_to_remove = random.sample(nodes, num_operations // 2)
        for node in nodes_to_remove:
            if node.id in graph.internal_nodes:
                graph.remove_node(node)

        assert len(graph.internal_nodes) == num_operations - len(nodes_to_remove)


@pytest.mark.asyncio
class TestGraphConcurrency:
    """Test concurrent graph operations"""

    async def test_concurrent_node_additions(self):
        graph = Graph()
        num_operations = 100

        async def add_node(i):
            node = create_test_node(f"Node{i}")
            graph.add_node(node)
            return node

        tasks = [add_node(i) for i in range(num_operations)]
        nodes = await asyncio.gather(*tasks)

        assert len(graph.internal_nodes) == num_operations
        assert all(node.id in graph.internal_nodes for node in nodes)

    async def test_concurrent_edge_additions(self):
        graph = Graph()
        num_nodes = 100

        # Create nodes first
        nodes = [create_test_node(f"Node{i}") for i in range(num_nodes)]
        for node in nodes:
            graph.add_node(node)

        async def add_edge(i):
            if i < num_nodes - 1:
                edge = Edge(head=nodes[i], tail=nodes[i + 1])
                graph.add_edge(edge)
                head_node = graph.internal_nodes[edge.head]
                tail_node = graph.internal_nodes[edge.tail]
                head_node.add_relation(edge, "out")
                tail_node.add_relation(edge, "in")
                return edge
            return None

        tasks = [add_edge(i) for i in range(num_nodes)]
        edges = await asyncio.gather(*tasks)
        edges = [edge for edge in edges if edge is not None]

        assert len(graph.internal_edges) == num_nodes - 1
        assert all(edge.id in graph.internal_edges for edge in edges)

    async def test_concurrent_mixed_operations(self):
        graph = Graph()
        num_operations = 100

        async def mixed_operation(i):
            # Add two nodes and connect them
            node1 = create_test_node(f"Node{i}a")
            node2 = create_test_node(f"Node{i}b")
            graph.add_node(node1)
            graph.add_node(node2)
            edge = Edge(head=node1, tail=node2)
            graph.add_edge(edge)
            head_node = graph.internal_nodes[edge.head]
            tail_node = graph.internal_nodes[edge.tail]
            head_node.add_relation(edge, "out")
            tail_node.add_relation(edge, "in")
            return node1, node2, edge

        tasks = [mixed_operation(i) for i in range(num_operations)]
        results = await asyncio.gather(*tasks)

        assert len(graph.internal_nodes) == num_operations * 2
        assert len(graph.internal_edges) == num_operations

        # Verify all nodes and edges are properly connected
        for node1, node2, edge in results:
            assert node1.id in graph.internal_nodes
            assert node2.id in graph.internal_nodes
            assert edge.id in graph.internal_edges
            assert edge.id in graph.node_edge_mapping[node1.id]["out"]
            assert edge.id in graph.node_edge_mapping[node2.id]["in"]


class TestGraphAdvancedOperations:
    """Test advanced graph operations"""

    def test_graph_merge(self):
        # Create first graph
        graph1 = Graph()
        nodes1 = [create_test_node("G1_Node1"), create_test_node("G1_Node2")]
        for node in nodes1:
            graph1.add_node(node)
        edge1 = Edge(head=nodes1[0], tail=nodes1[1])
        graph1.add_edge(edge1)
        nodes1[0].add_relation(edge1, "out")
        nodes1[1].add_relation(edge1, "in")

        # Create second graph
        graph2 = Graph()
        nodes2 = [create_test_node("G2_Node1"), create_test_node("G2_Node2")]
        for node in nodes2:
            graph2.add_node(node)
        edge2 = Edge(head=nodes2[0], tail=nodes2[1])
        graph2.add_edge(edge2)
        nodes2[0].add_relation(edge2, "out")
        nodes2[1].add_relation(edge2, "in")

        # Merge graphs
        merged = Graph()
        # Add nodes and edges from first graph
        for node in graph1.internal_nodes.values():
            merged.add_node(node)
        for edge in graph1.internal_edges.values():
            merged.add_edge(edge)
            head_node = merged.internal_nodes[edge.head]
            tail_node = merged.internal_nodes[edge.tail]
            head_node.add_relation(edge, "out")
            tail_node.add_relation(edge, "in")

        # Add nodes and edges from second graph
        for node in graph2.internal_nodes.values():
            merged.add_node(node)
        for edge in graph2.internal_edges.values():
            merged.add_edge(edge)
            head_node = merged.internal_nodes[edge.head]
            tail_node = merged.internal_nodes[edge.tail]
            head_node.add_relation(edge, "out")
            tail_node.add_relation(edge, "in")

        assert len(merged.internal_nodes) == 4
        assert len(merged.internal_edges) == 2

    def test_graph_with_large_properties(self):
        graph = Graph()
        large_content = "a" * 1000000  # 1MB string

        # Create node with large content
        node = create_test_node("LargeNode")
        node.content["large_property"] = large_content
        graph.add_node(node)

        # Create edge with large properties
        edge = Edge(head=node, tail=node)
        edge.update_property("large_property", large_content)
        graph.add_edge(edge)
        node.add_relation(edge, "out")
        node.add_relation(edge, "in")

        assert len(graph.internal_nodes) == 1
        assert len(graph.internal_edges) == 1
        assert len(graph.internal_nodes[node.id].content["large_property"]) == 1000000
        assert len(graph.internal_edges[edge.id].properties.get("large_property")) == 1000000

    def test_graph_stress(self):
        graph = Graph()
        num_nodes = 1000
        operations_per_node = 10

        nodes = [create_test_node(f"Node{i}") for i in range(num_nodes)]
        for node in nodes:
            graph.add_node(node)

        for _ in range(num_nodes * operations_per_node):
            operation = random.choice(["add_edge", "remove_node", "add_node"])

            if operation == "add_edge":
                head = random.choice(nodes)
                tail = random.choice(nodes)
                if head.id in graph.internal_nodes and tail.id in graph.internal_nodes:
                    try:
                        edge = Edge(head=head, tail=tail)
                        graph.add_edge(edge)
                        head_node = graph.internal_nodes[edge.head]
                        tail_node = graph.internal_nodes[edge.tail]
                        head_node.add_relation(edge, "out")
                        tail_node.add_relation(edge, "in")
                    except RelationError:
                        pass

            elif operation == "remove_node":
                node = random.choice(nodes)
                if node.id in graph.internal_nodes:
                    graph.remove_node(node)
                    nodes.remove(node)

            else:  # add_node
                node = create_test_node(f"NewNode{len(nodes)}")
                graph.add_node(node)
                nodes.append(node)

        for node in graph.internal_nodes.values():
            assert node.id in graph.node_edge_mapping

        for edge in graph.internal_edges.values():
            assert edge.head in graph.internal_nodes
            assert edge.tail in graph.internal_nodes
            assert edge.id in graph.node_edge_mapping[edge.head]["out"]
            assert edge.id in graph.node_edge_mapping[edge.tail]["in"]
