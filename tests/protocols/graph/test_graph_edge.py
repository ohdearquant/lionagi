import pytest
from pydantic import ConfigDict

from lionagi._errors import RelationError
from lionagi.protocols.types import Edge, EdgeCondition, Graph

from .helpers import create_test_node


class CustomEdgeCondition(EdgeCondition):
    """Custom edge condition for testing"""

    value: bool = True

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
    )

    async def apply(self, *args, **kwargs) -> bool:
        return self.value


@pytest.fixture
def edge_test_graph():
    """Fixture for testing edge functionality"""
    graph = Graph()

    # Create nodes
    node1 = create_test_node("EdgeNode1")
    node2 = create_test_node("EdgeNode2")
    node3 = create_test_node("EdgeNode3")

    # Add nodes
    graph.add_node(node1)
    graph.add_node(node2)
    graph.add_node(node3)

    return graph, node1, node2, node3


class TestEdgeBasics:
    """Test basic edge functionality"""

    def test_edge_creation(self, edge_test_graph):
        """Test creating edges with different configurations"""
        graph, node1, node2, _ = edge_test_graph

        # Basic edge
        edge = Edge(head=node1, tail=node2)
        graph.add_edge(edge)
        assert edge in graph.internal_edges

        # Edge with label
        edge_with_label = Edge(head=node1, tail=node2, label=["test_label"])
        graph.add_edge(edge_with_label)
        assert edge_with_label.properties.get("label") == ["test_label"]

        # Edge with properties
        edge_with_props = Edge(head=node1, tail=node2, weight=10, custom_prop="test")
        graph.add_edge(edge_with_props)
        assert edge_with_props.properties.get("weight") == 10
        assert edge_with_props.properties.get("custom_prop") == "test"

    def test_edge_properties(self, edge_test_graph):
        """Test edge property management"""
        graph, node1, node2, _ = edge_test_graph

        edge = Edge(head=node1, tail=node2)
        graph.add_edge(edge)

        # Add property
        edge.update_property("weight", 5)
        assert edge.properties.get("weight") == 5

        # Update property
        edge.update_property("weight", 10)
        assert edge.properties.get("weight") == 10

        # Remove property
        edge.properties.pop("weight")
        assert edge.properties.get("weight", None) is None

    def test_edge_validation(self, edge_test_graph):
        """Test edge validation"""
        graph, node1, node2, _ = edge_test_graph

        # Test with invalid head/tail
        with pytest.raises(RelationError):
            graph.add_edge(Edge(head=create_test_node("Invalid1"), tail=node2))

        with pytest.raises(RelationError):
            graph.add_edge(Edge(head=node1, tail=create_test_node("Invalid2")))

        # Test with missing nodes
        non_graph_node = create_test_node("NonGraphNode")
        with pytest.raises(RelationError):
            edge = Edge(head=node1, tail=non_graph_node)
            graph.add_edge(edge)


@pytest.mark.asyncio
class TestEdgeConditions:
    """Test edge conditions"""

    async def test_edge_condition_true(self, edge_test_graph):
        """Test edge condition that evaluates to True"""
        graph, node1, node2, _ = edge_test_graph

        condition = CustomEdgeCondition(value=True)
        edge = Edge(head=node1, tail=node2, condition=condition)
        graph.add_edge(edge)

        assert await edge.check_condition()

    async def test_edge_condition_false(self, edge_test_graph):
        """Test edge condition that evaluates to False"""
        graph, node1, node2, _ = edge_test_graph

        condition = CustomEdgeCondition(value=False)
        edge = Edge(head=node1, tail=node2, condition=condition)
        graph.add_edge(edge)

        assert not await edge.check_condition()

    async def test_edge_no_condition(self, edge_test_graph):
        """Test edge with no condition"""
        graph, node1, node2, _ = edge_test_graph

        edge = Edge(head=node1, tail=node2)
        graph.add_edge(edge)

        assert await edge.check_condition()

    async def test_custom_condition(self, edge_test_graph):
        """Test edge with custom condition class"""
        graph, node1, node2, _ = edge_test_graph

        class WeightCondition(EdgeCondition):
            min_weight: int
            source: dict

            async def apply(self, *args, **kwargs) -> bool:
                return self.source.get("weight", 0) >= self.min_weight

        condition = WeightCondition(min_weight=10, source={"weight": 15})
        edge = Edge(head=node1, tail=node2, condition=condition)
        graph.add_edge(edge)

        assert await edge.check_condition()

        # Update source to fail condition
        edge.update_condition_source({"weight": 5})
        assert not await edge.check_condition()


class TestEdgeTypes:
    """Test different edge types and configurations"""

    def test_multi_label_edge(self, edge_test_graph):
        """Test edge with multiple labels"""
        graph, node1, node2, _ = edge_test_graph

        edge = Edge(head=node1, tail=node2, label=["label1", "label2", "label3"])
        graph.add_edge(edge)

        assert isinstance(edge.properties.get("label"), list)
        assert len(edge.properties.get("label")) == 3
        assert "label1" in edge.properties.get("label")

    def test_weighted_edge(self, edge_test_graph):
        """Test edge with weight property"""
        graph, node1, node2, _ = edge_test_graph

        edge = Edge(head=node1, tail=node2)
        edge.update_property("weight", 5.5)
        graph.add_edge(edge)

        assert edge.properties.get("weight") == 5.5

    def test_custom_edge_type(self, edge_test_graph):
        """Test custom edge type"""
        graph, node1, node2, _ = edge_test_graph

        class WeightedEdge(Edge):
            def __init__(self, head, tail, weight: float):
                super().__init__(head=head, tail=tail)
                self.update_property("weight", weight)

            @property
            def weight(self) -> float:
                return self.properties.get("weight")

            @weight.setter
            def weight(self, value: float):
                self.update_property("weight", value)

        edge = WeightedEdge(head=node1, tail=node2, weight=10.0)
        graph.add_edge(edge)

        assert edge.weight == 10.0
        edge.weight = 15.0
        assert edge.weight == 15.0


class TestEdgeOperations:
    """Test edge operations in graph context"""

    def test_parallel_edges(self, edge_test_graph):
        """Test parallel edges between same nodes"""
        graph, node1, node2, _ = edge_test_graph

        edge1 = Edge(head=node1, tail=node2)
        edge1.update_property("type", "type1")
        edge2 = Edge(head=node1, tail=node2)
        edge2.update_property("type", "type2")

        graph.add_edge(edge1)
        graph.add_edge(edge2)

        edges = graph.find_node_edge(node1, direction="out")
        assert len(edges) == 2
        assert any(e.properties.get("type") == "type1" for e in edges)
        assert any(e.properties.get("type") == "type2" for e in edges)

    def test_bidirectional_edges(self, edge_test_graph):
        """Test bidirectional edges"""
        graph, node1, node2, _ = edge_test_graph

        edge1 = Edge(head=node1, tail=node2)
        edge2 = Edge(head=node2, tail=node1)

        graph.add_edge(edge1)
        graph.add_edge(edge2)

        assert len(graph.find_node_edge(node1)) == 2
        assert len(graph.find_node_edge(node2)) == 2

    def test_self_loop_edge(self, edge_test_graph):
        """Test self-loop edge"""
        graph, node1, _, _ = edge_test_graph

        edge = Edge(head=node1, tail=node1)
        graph.add_edge(edge)

        assert edge.id in graph.node_edge_mapping[node1.id]["in"]
        assert edge.id in graph.node_edge_mapping[node1.id]["out"]

    def test_edge_removal_cleanup(self, edge_test_graph):
        """Test proper cleanup after edge removal"""
        graph, node1, node2, _ = edge_test_graph

        edge = Edge(head=node1, tail=node2)
        graph.add_edge(edge)
        graph.remove_edge(edge)

        assert edge.id not in graph.internal_edges
        assert edge.id not in graph.node_edge_mapping[node1.id]["out"]
        assert edge.id not in graph.node_edge_mapping[node2.id]["in"]


# ---------------------------------------------------------------------------
# Coverage gap tests for edge.py lines 97, 101, 107, 132-141
# ---------------------------------------------------------------------------

from lionagi.protocols.generic.element import Element


def _make_nodes():
    return Element(), Element()


class TestEdgeLabelInitCoverage:
    """Edge.__init__ label-handling paths (lines 97, 101)."""

    def test_string_label_wraps_in_list(self):
        """Line 97: label='str' → kwargs['label'] = ['str']."""
        h, t = _make_nodes()
        e = Edge(h, t, label="important")
        assert e.label == ["important"]

    def test_invalid_label_raises(self):
        """Line 101: label=[1, 2] → ValueError."""
        h, t = _make_nodes()
        with pytest.raises(ValueError, match="Label must be a string or a list of strings"):
            Edge(h, t, label=[1, 2])

    def test_invalid_label_dict_raises(self):
        """Line 101: label=dict → ValueError."""
        h, t = _make_nodes()
        with pytest.raises(ValueError, match="Label must be a string or a list of strings"):
            Edge(h, t, label={"a": 1})


class TestEdgeSerializerCoverage:
    """Edge._serialize_id field_serializer (line 107)."""

    def test_model_dump_json_mode_serializes_uuid_as_string(self):
        """Line 107: field_serializer converts head/tail UUID → str."""
        h, t = _make_nodes()
        e = Edge(h, t)
        data = e.model_dump(mode="json")
        assert isinstance(data["head"], str)
        assert isinstance(data["tail"], str)
        assert data["head"] == str(e.head)
        assert data["tail"] == str(e.tail)


class TestEdgeLabelSetterCoverage:
    """Edge.label setter paths (lines 132-141)."""

    def test_set_none_clears_label(self):
        """Lines 132-134: label=None → properties['label'] = []."""
        h, t = _make_nodes()
        e = Edge(h, t, label=["old"])
        e.label = None
        assert e.properties["label"] == []

    def test_set_empty_list_clears_label(self):
        """Lines 132-134: empty list (falsy) → properties['label'] = []."""
        h, t = _make_nodes()
        e = Edge(h, t, label=["old"])
        e.label = []
        assert e.properties["label"] == []

    def test_set_string_label_wraps(self):
        """Lines 135-137: str → properties['label'] = [str]."""
        h, t = _make_nodes()
        e = Edge(h, t)
        e.label = "newtag"
        assert e.properties["label"] == ["newtag"]

    def test_set_list_of_strings(self):
        """Lines 138-140: list[str] stored directly."""
        h, t = _make_nodes()
        e = Edge(h, t)
        e.label = ["a", "b"]
        assert e.properties["label"] == ["a", "b"]

    def test_set_invalid_label_raises(self):
        """Line 141: non-string list → ValueError."""
        h, t = _make_nodes()
        e = Edge(h, t)
        with pytest.raises(ValueError, match="Label must be a string or a list of strings"):
            e.label = [1, 2]

    def test_set_mixed_type_list_raises(self):
        """Line 141: mixed-type list → ValueError."""
        h, t = _make_nodes()
        e = Edge(h, t)
        with pytest.raises(ValueError, match="Label must be a string or a list of strings"):
            e.label = ["valid", 42]


class TestEdgeConditionInitCoverage:
    """Edge.__init__ condition validation (lines 90-93)."""

    def test_invalid_condition_raises(self):
        """Lines 90-92: condition not a Condition subclass → ValueError."""
        h, t = _make_nodes()
        with pytest.raises(ValueError, match="condition must be a Condition subclass"):
            Edge(h, t, condition="not_a_condition")

    def test_invalid_condition_dict_raises(self):
        """Lines 90-92: dict condition → ValueError."""
        h, t = _make_nodes()
        with pytest.raises(ValueError, match="condition must be a Condition subclass"):
            Edge(h, t, condition={"apply": "something"})


class TestEdgeConditionSetterCoverage:
    """Edge.condition setter (lines 123-128)."""

    def test_set_valid_condition(self):
        """Lines 123-128: valid Condition → stored in properties."""
        h, t = _make_nodes()
        e = Edge(h, t)
        cond = CustomEdgeCondition(source="test")
        e.condition = cond
        assert e.condition is cond

    def test_set_none_condition(self):
        """Lines 123-128: None → properties['condition'] = None."""
        h, t = _make_nodes()
        e = Edge(h, t)
        e.condition = None
        assert e.properties["condition"] is None

    def test_set_invalid_condition_raises(self):
        """Lines 123-124: non-Condition value → ValueError."""
        h, t = _make_nodes()
        e = Edge(h, t)
        with pytest.raises(ValueError, match="condition must be a Condition subclass"):
            e.condition = "bad_value"
