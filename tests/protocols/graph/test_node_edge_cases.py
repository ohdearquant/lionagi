"""Coverage boost tests for Node: embedding validation, serialization, adaptation, and registration."""

import builtins
import importlib.abc
import sys
import types
from unittest.mock import patch

import orjson
import pytest
from pydantic import BaseModel

from lionagi._class_registry import LION_CLASS_REGISTRY
from lionagi.protocols.generic.element import Element
from lionagi.protocols.graph.node import Node, _ensure_postgres_adapter


class TestNodeBasicFunctionality:
    """Basic Node functionality."""

    def test_node_creation_basic(self):
        """Basic Node creation."""
        node = Node()

        assert isinstance(node, Node)
        assert node.content is None
        assert node.embedding is None

    def test_node_creation_with_content(self):
        """Test Node creation with content."""
        content = {"test": "data"}
        node = Node(content=content)

        assert node.content == content

    def test_node_creation_with_embedding(self):
        """Test Node creation with valid embedding."""
        embedding = [1.0, 2.0, 3.0]
        node = Node(embedding=embedding)

        assert node.embedding == embedding

    def test_node_inherits_from_element(self):
        """Node inherits from Element and has ID."""
        node = Node()

        assert isinstance(node, Element)
        assert hasattr(node, "id")
        assert node.id is not None

    def test_node_has_element_properties(self):
        """Test Node has Element properties like ID."""
        node = Node()

        # Should have Element properties
        assert hasattr(node, "id")
        assert node.id is not None


class TestNodeEmbeddingValidation:
    """Test Node embedding field validation."""

    def test_embedding_valid_list(self):
        """Embedding with valid float list."""
        embedding = [1.0, 2.5, -3.7, 0.0]
        node = Node(embedding=embedding)

        assert node.embedding == embedding

    def test_embedding_valid_list_integers(self):
        """Embedding with integer list (converted to float)."""
        embedding = [1, 2, 3]
        node = Node(embedding=embedding)

        assert node.embedding == [1.0, 2.0, 3.0]

    def test_embedding_none(self):
        """Embedding with None value."""
        node = Node(embedding=None)

        assert node.embedding is None

    def test_embedding_valid_json_string(self):
        """Embedding with valid JSON string."""
        embedding_list = [1.5, 2.5, 3.5]
        embedding_json = orjson.dumps(embedding_list).decode()
        node = Node(embedding=embedding_json)

        assert node.embedding == embedding_list

    def test_embedding_invalid_json_string(self):
        """Embedding with invalid JSON string."""
        with pytest.raises(ValueError, match="Invalid embedding string"):
            Node(embedding="invalid json")

    def test_embedding_json_non_list(self):
        """Embedding with JSON string that's not a list."""
        json_dict = orjson.dumps({"not": "a list"}).decode()

        with pytest.raises(ValueError, match="Invalid embedding string"):
            Node(embedding=json_dict)

    def test_embedding_invalid_list_values(self):
        """Embedding with list containing non-numeric values."""
        with pytest.raises(ValueError, match="Invalid embedding list"):
            Node(embedding=[1.0, "invalid", 3.0])

    def test_embedding_invalid_type(self):
        """Embedding with completely invalid type."""
        with pytest.raises(ValueError, match="Invalid embedding type"):
            Node(embedding={"invalid": "type"})

    def test_embedding_empty_list(self):
        """Embedding with empty list."""
        node = Node(embedding=[])

        assert node.embedding == []


class TestNodeContentSerialization:
    """Test Node content serialization."""

    def test_content_serialization_element(self):
        """Content serialization with Element object."""
        inner_element = Element()
        node = Node(content=inner_element)

        # Get the serialized content
        serialized = node.model_dump()

        # Should call to_dict() on Element
        assert isinstance(serialized["content"], dict)

    def test_content_serialization_basemodel(self):
        """Content serialization with BaseModel object."""

        class TestModel(BaseModel):
            value: str = "test"

        test_model = TestModel()
        node = Node(content=test_model)

        # Get the serialized content
        serialized = node.model_dump()

        # Should call model_dump() on BaseModel
        assert serialized["content"] == {"value": "test"}

    def test_content_serialization_regular_object(self):
        """Content serialization with regular object."""
        content = {"regular": "dict"}
        node = Node(content=content)

        serialized = node.model_dump()

        # Should return as-is for regular objects
        assert serialized["content"] == content

    def test_content_validation_with_lion_class(self):
        """Content validation with lion_class in metadata."""
        # Create a mock dict that looks like an Element
        content_dict = {
            "id": "test_id",
            "metadata": {"lion_class": "Element"},
            "content": "test",
        }

        # Mock Element.from_dict to avoid complex setup
        with patch.object(Element, "from_dict") as mock_from_dict:
            mock_element = Element()
            mock_from_dict.return_value = mock_element

            node = Node(content=content_dict)

            # Should have called Element.from_dict
            mock_from_dict.assert_called_once_with(content_dict)
            assert node.content == mock_element

    def test_content_validation_regular_dict(self):
        """Content validation with regular dict."""
        content_dict = {"regular": "dict"}
        node = Node(content=content_dict)

        # Should pass through unchanged
        assert node.content == content_dict


class TestNodeAdaptation:
    """Test Node adaptation methods."""

    def test_adapt_to_basic(self):
        """Basic adapt_to functionality."""
        node = Node(content="test")

        # Mock the parent adapt_to method
        with patch.object(Node.__bases__[-1], "adapt_to") as mock_adapt:
            mock_adapt.return_value = {"adapted": "data"}

            result = node.adapt_to("test_adapter")

            mock_adapt.assert_called_once()
            args, kwargs = mock_adapt.call_args
            assert kwargs["adapt_meth"] == "to_dict"
            assert kwargs["adapt_kw"] == {"mode": "db"}

    def test_adapt_from_basic(self):
        """Basic adapt_from functionality."""
        test_data = {"test": "data"}

        # Mock the parent adapt_from method
        with patch.object(Node, "adapt_from") as mock_adapt:
            mock_node = Node()
            mock_adapt.return_value = mock_node

            # Need to call on a subclass to avoid infinite recursion
            class TestNode(Node):
                pass

            result = TestNode.adapt_from(test_data, "test_adapter")

            # Should have set adapt_meth to from_dict
            assert isinstance(result, Node)

    @pytest.mark.asyncio
    async def test_adapt_to_async_basic(self):
        """Basic async adapt_to functionality."""
        node = Node(content="test")

        # Mock the parent adapt_to_async method
        with patch.object(Node.__bases__[-2], "adapt_to_async") as mock_adapt:
            mock_adapt.return_value = {"adapted": "data"}

            result = await node.adapt_to_async("test_adapter")

            mock_adapt.assert_called_once()
            args, kwargs = mock_adapt.call_args
            assert kwargs["adapt_meth"] == "to_dict"
            assert kwargs["adapt_kw"] == {"mode": "db"}

    @pytest.mark.asyncio
    async def test_adapt_to_async_postgres(self):
        """Async adapt_to with postgres adapter."""
        node = Node(content="test")

        with patch("lionagi.protocols.graph.node._ensure_postgres_adapter") as mock_ensure:
            with patch.object(Node.__bases__[-2], "adapt_to_async") as mock_adapt:
                mock_adapt.return_value = {"adapted": "data"}

                result = await node.adapt_to_async("lionagi_async_pg")

                # Should have called _ensure_postgres_adapter
                mock_ensure.assert_called_once()
                mock_adapt.assert_called_once()

    @pytest.mark.asyncio
    async def test_adapt_from_async_basic(self):
        """Basic async adapt_from functionality."""
        test_data = {"test": "data"}

        with patch.object(Node, "adapt_from_async") as mock_adapt:
            mock_node = Node()
            mock_adapt.return_value = mock_node

            # Need to call on a subclass to avoid infinite recursion
            class TestNode(Node):
                pass

            result = await TestNode.adapt_from_async(test_data, "test_adapter")

            assert isinstance(result, Node)

    @pytest.mark.asyncio
    async def test_adapt_from_async_postgres(self):
        """Async adapt_from with postgres adapter."""
        test_data = {"test": "data"}

        with patch("lionagi.protocols.graph.node._ensure_postgres_adapter") as mock_ensure:
            # Need to patch the superclass method to avoid infinite recursion
            with patch.object(Node.__bases__[-2], "adapt_from_async") as mock_adapt:
                mock_node = Node()
                mock_adapt.return_value = mock_node

                result = await Node.adapt_from_async(test_data, "lionagi_async_pg")

                # Should have called _ensure_postgres_adapter
                mock_ensure.assert_called_once()


class TestNodeRegistration:
    """Test Node subclass registration."""

    def test_subclass_registration(self):
        """Node subclasses are registered in class registry."""

        # Create a unique subclass to test registration
        class TestNodeSubclass(Node):
            test_field: str = "test"

        # Check that it was registered
        class_name = TestNodeSubclass.class_name(full=True)
        assert class_name in LION_CLASS_REGISTRY
        assert LION_CLASS_REGISTRY[class_name] == TestNodeSubclass

    def test_pydantic_init_subclass_calls_super(self):
        """__pydantic_init_subclass__ calls super()."""
        # Mock the super().__pydantic_init_subclass__ to verify it's called
        with patch.object(Node.__bases__[0], "__pydantic_init_subclass__") as mock_super:

            class TestSubclass(Node):
                pass

            # Should have called super().__pydantic_init_subclass__
            mock_super.assert_called_once()


_HUB_ADAPTER = "lionhub.integrations.postgres_adapter"


class _BlockTheHub(importlib.abc.MetaPathFinder):
    """Answer every lionhub import the way an install without the hub does."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "lionhub" or fullname.startswith("lionhub."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


class TestPostgresAdapterIntegration:
    """Registration of the async PostgreSQL adapter on first use of its key.

    Every test stands the hub module in with a fake or blocks the hub, so the suite needs
    neither lionhub nor the postgres extra.
    """

    @pytest.fixture
    def unchecked(self, monkeypatch):
        monkeypatch.setattr(Node, "_postgres_adapter_checked", False, raising=False)
        registered = []
        monkeypatch.setattr(Node, "register_async_adapter", registered.append)
        return registered

    @pytest.fixture
    def extra_present(self, monkeypatch):
        monkeypatch.setattr("lionagi.adapters._utils.check_async_postgres_available", lambda: True)

    @pytest.fixture
    def hub_adapter(self, monkeypatch):
        built = []

        def create_lionagi_async_postgres_adapter():
            adapter = type("FakeAdapter", (), {"obj_key": "lionagi_async_pg"})
            built.append(adapter)
            return adapter

        module = types.ModuleType(_HUB_ADAPTER)
        module.create_lionagi_async_postgres_adapter = create_lionagi_async_postgres_adapter
        monkeypatch.setitem(sys.modules, _HUB_ADAPTER, module)
        return built

    @pytest.fixture
    def hub_absent(self, monkeypatch):
        for name in list(sys.modules):
            if name == "lionhub" or name.startswith("lionhub."):
                monkeypatch.delitem(sys.modules, name)
        monkeypatch.setattr(sys, "meta_path", [_BlockTheHub(), *sys.meta_path])

    def test_returns_at_once_when_already_registered(self, monkeypatch):
        monkeypatch.setattr(Node, "_postgres_adapter_checked", True, raising=False)
        monkeypatch.setattr(
            "lionagi.adapters._utils.check_async_postgres_available",
            lambda: pytest.fail("checked again after registration"),
        )
        _ensure_postgres_adapter()

    def test_registers_the_hub_adapter_once(self, unchecked, extra_present, hub_adapter):
        _ensure_postgres_adapter()
        _ensure_postgres_adapter()

        assert unchecked == hub_adapter
        assert len(hub_adapter) == 1
        assert Node._postgres_adapter_checked is True

    def test_missing_extra_raises_the_extra_error_and_registers_nothing(
        self, monkeypatch, unchecked, hub_adapter
    ):
        missing = ImportError("postgres extra missing")
        monkeypatch.setattr(
            "lionagi.adapters._utils.check_async_postgres_available", lambda: missing
        )

        with pytest.raises(ImportError) as info:
            _ensure_postgres_adapter()

        assert info.value is missing
        assert unchecked == []
        assert hub_adapter == []
        assert Node._postgres_adapter_checked is False

    def test_absent_hub_raises_the_extra_error_chained_to_the_missing_hub(
        self, unchecked, extra_present, hub_absent
    ):
        from lionagi.adapters._utils import postgres_extra_error

        with pytest.raises(ImportError) as info:
            _ensure_postgres_adapter()

        assert type(info.value) is ImportError
        assert info.value.args == postgres_extra_error().args
        assert isinstance(info.value.__cause__, ModuleNotFoundError)
        assert info.value.__cause__.name == "lionhub"
        assert unchecked == []
        assert Node._postgres_adapter_checked is False

    def test_a_missing_module_other_than_the_hub_at_import_propagates(
        self, monkeypatch, unchecked, extra_present
    ):
        real_import = builtins.__import__

        def importing(name, *args, **kwargs):
            if name == _HUB_ADAPTER:
                raise ModuleNotFoundError(
                    "No module named 'pydapter.extras'", name="pydapter.extras"
                )
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", importing)

        with pytest.raises(ModuleNotFoundError) as info:
            _ensure_postgres_adapter()

        assert info.value.name == "pydapter.extras"
        assert info.value.__cause__ is None
        assert unchecked == []
        assert Node._postgres_adapter_checked is False

    def test_a_missing_module_while_building_the_adapter_propagates(
        self, monkeypatch, unchecked, extra_present
    ):
        module = types.ModuleType(_HUB_ADAPTER)

        def create_lionagi_async_postgres_adapter():
            raise ModuleNotFoundError("No module named 'pydapter.extras'", name="pydapter.extras")

        module.create_lionagi_async_postgres_adapter = create_lionagi_async_postgres_adapter
        monkeypatch.setitem(sys.modules, _HUB_ADAPTER, module)

        with pytest.raises(ModuleNotFoundError) as info:
            _ensure_postgres_adapter()

        assert info.value.name == "pydapter.extras"
        assert info.value.__cause__ is None
        assert unchecked == []
        assert Node._postgres_adapter_checked is False

    def test_a_failure_to_build_the_adapter_propagates(self, monkeypatch, unchecked, extra_present):
        module = types.ModuleType(_HUB_ADAPTER)

        def create_lionagi_async_postgres_adapter():
            raise RuntimeError("adapter construction failed")

        module.create_lionagi_async_postgres_adapter = create_lionagi_async_postgres_adapter
        monkeypatch.setitem(sys.modules, _HUB_ADAPTER, module)

        with pytest.raises(RuntimeError, match="adapter construction failed"):
            _ensure_postgres_adapter()

        assert unchecked == []
        assert Node._postgres_adapter_checked is False

    def test_a_call_after_a_failure_tries_again(self, monkeypatch, unchecked, hub_adapter):
        answers = iter([ImportError("postgres extra missing"), True])
        monkeypatch.setattr(
            "lionagi.adapters._utils.check_async_postgres_available", lambda: next(answers)
        )

        with pytest.raises(ImportError):
            _ensure_postgres_adapter()
        _ensure_postgres_adapter()

        assert unchecked == hub_adapter
        assert len(hub_adapter) == 1
        assert Node._postgres_adapter_checked is True


class TestNodeIntegration:
    """Integration tests for Node functionality."""

    def test_node_full_lifecycle(self):
        """Complete Node lifecycle with various features."""
        # Create node with content and embedding
        content = {"type": "test", "data": [1, 2, 3]}
        embedding = [0.1, 0.2, 0.3]

        node = Node(content=content, embedding=embedding)

        # Validate basic properties
        assert isinstance(node, Node)
        assert isinstance(node, Element)
        assert hasattr(node, "id")
        assert node.content == content
        assert node.embedding == embedding

        # Test serialization
        serialized = node.model_dump()
        assert serialized["content"] == content
        assert serialized["embedding"] == embedding

        # Test that it's properly registered as a Node
        assert isinstance(node, Node)
        assert isinstance(node, Element)

    def test_node_with_complex_content(self):
        """Test Node with complex nested content."""

        class ComplexModel(BaseModel):
            name: str = "test"
            values: list[int] = [1, 2, 3]

        complex_content = ComplexModel()
        node = Node(content=complex_content)

        # Should serialize the BaseModel correctly
        serialized = node.model_dump()
        assert serialized["content"] == {"name": "test", "values": [1, 2, 3]}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# Node embedding: JSON string must decode to a list of floats


class TestNodeEmbeddingJsonString:
    """Validate Node._parse_embedding behaviour for JSON-encoded strings."""

    def test_embedding_json_string_decoded_to_float_list(self):
        """A valid JSON-encoded float array is accepted and decoded."""
        import json

        raw = json.dumps([1.0, 2.5, 3.14])
        node = Node(embedding=raw)
        assert node.embedding == [1.0, 2.5, 3.14]
        assert all(isinstance(v, float) for v in node.embedding)

    def test_embedding_json_string_with_ints_coerced_to_float(self):
        """Integer values in a JSON-encoded array are coerced to float."""
        import json

        raw = json.dumps([1, 2, 3])
        node = Node(embedding=raw)
        assert node.embedding == [1.0, 2.0, 3.0]
        assert all(isinstance(v, float) for v in node.embedding)

    def test_embedding_json_dict_string_rejected(self):
        """A JSON object (dict) encoded as string must be rejected."""
        import json

        import pytest
        from pydantic import ValidationError

        raw = json.dumps({"a": 1.0})
        with pytest.raises((ValidationError, ValueError)):
            Node(embedding=raw)

    def test_embedding_non_json_string_rejected(self):
        """A plain non-JSON string must be rejected."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises((ValidationError, ValueError)):
            Node(embedding="not-a-json-list")
