# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import pytest

from lionagi.protocols.messages.action_response import (
    ActionResponse,
    ActionResponseContent,
)
from lionagi.protocols.messages.message import MessageRole

# ============================================================================
# ActionResponseContent Tests
# ============================================================================


def test_action_response_content_initialization_defaults():
    """Test ActionResponseContent initialization with default values."""
    content = ActionResponseContent()

    assert content.function == ""
    assert content.arguments == {}
    assert content.output is None
    assert content.action_request_id is None


def test_action_response_content_initialization_with_values():
    """Test ActionResponseContent initialization with explicit values."""
    content = ActionResponseContent(
        function="test_function",
        arguments={"arg1": "value1", "arg2": 42},
        output={"result": "success", "data": [1, 2, 3]},
        action_request_id="request_123",
    )

    assert content.function == "test_function"
    assert content.arguments == {"arg1": "value1", "arg2": 42}
    assert content.output == {"result": "success", "data": [1, 2, 3]}
    assert content.action_request_id == "request_123"


def test_action_response_content_dataclass_immutable_slots():
    """Test that ActionResponseContent uses slots for memory efficiency."""
    content = ActionResponseContent(function="test")

    # Verify slots are defined
    assert hasattr(ActionResponseContent, "__slots__")

    # Verify we cannot add arbitrary attributes
    with pytest.raises(AttributeError):
        content.arbitrary_attribute = "value"


def test_action_response_content_arguments_default_factory():
    """Test that arguments field uses default factory for independent instances."""
    content1 = ActionResponseContent()
    content2 = ActionResponseContent()

    # Modify one instance's arguments
    content1.arguments["key"] = "value"

    # Verify the other instance is not affected
    assert "key" not in content2.arguments
    assert content1.arguments != content2.arguments


def test_action_response_content_rendered_property():
    """Test the rendered property formats content as YAML."""
    content = ActionResponseContent(
        function="calculate_sum",
        arguments={"a": 10, "b": 20},
        output=30,
    )

    rendered = content.rendered

    # Verify it's a string
    assert isinstance(rendered, str)

    # Verify it contains expected YAML structure
    assert "Function: calculate_sum" in rendered
    assert "Arguments:" in rendered
    assert "a: 10" in rendered
    assert "b: 20" in rendered
    assert "Output: 30" in rendered


def test_action_response_content_rendered_with_complex_output():
    """Test rendered property with complex nested output."""
    content = ActionResponseContent(
        function="process_data",
        arguments={"input": "data.json"},
        output={
            "status": "complete",
            "records_processed": 150,
            "errors": [],
            "summary": {"total": 150, "success": 150, "failed": 0},
        },
    )

    rendered = content.rendered

    assert isinstance(rendered, str)
    assert "Function: process_data" in rendered
    assert "Arguments:" in rendered
    assert "Output:" in rendered


def test_action_response_content_rendered_with_none_output():
    """Test rendered property when output is None."""
    content = ActionResponseContent(
        function="void_function",
        arguments={},
        output=None,
    )

    rendered = content.rendered

    assert isinstance(rendered, str)
    assert "Function: void_function" in rendered
    # Output is None, so minimal_yaml drops it - don't assert presence


# ============================================================================
# ActionResponseContent.from_dict() Tests - Backward Compatibility
# ============================================================================


def test_action_response_content_from_dict_flat_structure():
    """Test from_dict with flat dictionary structure (new format)."""
    data = {
        "function": "test_function",
        "arguments": {"param": "value"},
        "output": {"result": "success"},
        "action_request_id": "request_456",
    }

    content = ActionResponseContent.from_dict(data)

    assert content.function == "test_function"
    assert content.arguments == {"param": "value"}
    assert content.output == {"result": "success"}
    assert content.action_request_id == "request_456"


def test_action_response_content_from_dict_nested_structure():
    """Test from_dict with nested 'action_response' key (old format - backward compat)."""
    data = {
        "action_response": {
            "function": "legacy_function",
            "arguments": {"old_param": "old_value"},
            "output": {"legacy": "data"},
        },
        "action_request_id": "legacy_request_789",
    }

    content = ActionResponseContent.from_dict(data)

    assert content.function == "legacy_function"
    assert content.arguments == {"old_param": "old_value"}
    assert content.output == {"legacy": "data"}
    assert content.action_request_id == "legacy_request_789"


def test_action_response_content_from_dict_missing_fields():
    """Test from_dict with missing optional fields."""
    data = {
        "action_response": {
            "function": "minimal_function",
        }
    }

    content = ActionResponseContent.from_dict(data)

    assert content.function == "minimal_function"
    assert content.arguments == {}
    assert content.output is None
    assert content.action_request_id is None


def test_action_response_content_from_dict_empty_dict():
    """Test from_dict with completely empty dictionary."""
    data = {}

    content = ActionResponseContent.from_dict(data)

    assert content.function == ""
    assert content.arguments == {}
    assert content.output is None
    assert content.action_request_id is None


def test_action_response_content_from_dict_action_request_id_coercion():
    """Test that action_request_id is coerced to string if provided."""
    data = {
        "function": "test",
        "action_request_id": 12345,  # Integer, should be converted to string
    }

    content = ActionResponseContent.from_dict(data)

    assert content.action_request_id == "12345"
    assert isinstance(content.action_request_id, str)


def test_action_response_content_from_dict_nested_with_partial_fields():
    """Test from_dict with nested structure but partial fields."""
    data = {
        "action_response": {
            "function": "partial_function",
            "output": "simple_output",
            # Missing 'arguments'
        },
        # Missing 'action_request_id'
    }

    content = ActionResponseContent.from_dict(data)

    assert content.function == "partial_function"
    assert content.arguments == {}
    assert content.output == "simple_output"
    assert content.action_request_id is None


# ============================================================================
# ActionResponse Tests
# ============================================================================


def test_action_response_initialization_with_content_dict():
    """Test ActionResponse initialization with content as dictionary."""
    response = ActionResponse(
        content={
            "function": "test_function",
            "arguments": {"arg": "value"},
            "output": "result",
        }
    )

    assert response.role == MessageRole.ACTION
    assert isinstance(response.content, ActionResponseContent)
    assert response.content.function == "test_function"
    assert response.content.arguments == {"arg": "value"}
    assert response.content.output == "result"


def test_action_response_initialization_with_content_object():
    """Test ActionResponse initialization with ActionResponseContent object."""
    content = ActionResponseContent(
        function="direct_function",
        arguments={"param": 123},
        output=True,
    )

    response = ActionResponse(content=content)

    assert response.role == MessageRole.ACTION
    assert response.content is content
    assert response.content.function == "direct_function"


def test_action_response_initialization_with_none_content():
    """Test ActionResponse initialization with None content."""
    response = ActionResponse(content=None)

    assert response.role == MessageRole.ACTION
    assert isinstance(response.content, ActionResponseContent)
    assert response.content.function == ""
    assert response.content.arguments == {}
    assert response.content.output is None


def test_action_response_content_validator_with_nested_dict():
    """Test ActionResponse content validator with nested action_response dict."""
    response = ActionResponse(
        content={
            "action_response": {
                "function": "validated_function",
                "arguments": {"key": "value"},
                "output": {"status": "ok"},
            },
            "action_request_id": "req_999",
        }
    )

    assert isinstance(response.content, ActionResponseContent)
    assert response.content.function == "validated_function"
    assert response.content.arguments == {"key": "value"}
    assert response.content.output == {"status": "ok"}
    assert response.content.action_request_id == "req_999"


def test_action_response_content_validator_invalid_type():
    """Test ActionResponse content validator rejects invalid types."""
    with pytest.raises(TypeError, match="content must be dict or ActionResponseContent"):
        ActionResponse(content="invalid_string")

    with pytest.raises(TypeError, match="content must be dict or ActionResponseContent"):
        ActionResponse(content=12345)

    with pytest.raises(TypeError, match="content must be dict or ActionResponseContent"):
        ActionResponse(content=["list", "of", "items"])


def test_action_response_role_is_action():
    """Test that ActionResponse has correct role."""
    response = ActionResponse(content=ActionResponseContent())

    assert response.role == MessageRole.ACTION
    assert response.role.value == "action"


def test_action_response_with_action_request_id():
    """Test ActionResponse properly stores action_request_id."""
    response = ActionResponse(
        content={
            "function": "linked_function",
            "arguments": {},
            "output": None,
            "action_request_id": "original_request_id",
        }
    )

    assert response.content.action_request_id == "original_request_id"


def test_action_response_rendered_content():
    """Test that ActionResponse can access rendered content."""
    response = ActionResponse(
        content={
            "function": "render_test",
            "arguments": {"x": 1, "y": 2},
            "output": 3,
        }
    )

    rendered = response.content.rendered

    assert isinstance(rendered, str)
    assert "Function: render_test" in rendered
    assert "Arguments:" in rendered
    assert "Output: 3" in rendered


def test_action_response_content_access():
    """Test direct access to ActionResponseContent fields through ActionResponse."""
    response = ActionResponse(
        content={
            "function": "access_test",
            "arguments": {"param1": "value1"},
            "output": {"result": 42},
            "action_request_id": "test_req_id",
        }
    )

    # Access through content attribute
    assert response.content.function == "access_test"
    assert response.content.arguments == {"param1": "value1"}
    assert response.content.output == {"result": 42}
    assert response.content.action_request_id == "test_req_id"


# ============================================================================
# Edge Cases and Integration Tests
# ============================================================================


def test_action_response_content_with_empty_strings():
    """Test ActionResponseContent with empty string values."""
    content = ActionResponseContent(
        function="",
        arguments={},
        output="",
        action_request_id="",
    )

    assert content.function == ""
    assert content.arguments == {}
    assert content.output == ""
    assert content.action_request_id == ""


def test_action_response_content_with_complex_arguments():
    """Test ActionResponseContent with nested/complex arguments."""
    complex_args = {
        "config": {
            "nested": {
                "deep": {
                    "value": 123,
                }
            }
        },
        "list_param": [1, 2, 3, {"key": "value"}],
        "tuple_param": (1, 2, 3),
    }

    content = ActionResponseContent(
        function="complex_function",
        arguments=complex_args,
        output=None,
    )

    assert content.arguments == complex_args
    assert content.arguments["config"]["nested"]["deep"]["value"] == 123
    assert content.arguments["list_param"][-1] == {"key": "value"}


def test_action_response_content_output_types():
    """Test ActionResponseContent with various output types."""
    # String output
    content_str = ActionResponseContent(function="f", output="string output")
    assert content_str.output == "string output"

    # Integer output
    content_int = ActionResponseContent(function="f", output=42)
    assert content_int.output == 42

    # Float output
    content_float = ActionResponseContent(function="f", output=3.14159)
    assert content_float.output == 3.14159

    # Boolean output
    content_bool = ActionResponseContent(function="f", output=True)
    assert content_bool.output is True

    # List output
    content_list = ActionResponseContent(function="f", output=[1, 2, 3])
    assert content_list.output == [1, 2, 3]

    # Dict output
    content_dict = ActionResponseContent(function="f", output={"key": "value"})
    assert content_dict.output == {"key": "value"}

    # None output
    content_none = ActionResponseContent(function="f", output=None)
    assert content_none.output is None


def test_action_response_dataclass_equality():
    """Test ActionResponseContent equality comparison."""
    content1 = ActionResponseContent(
        function="test",
        arguments={"a": 1},
        output="result",
        action_request_id="req1",
    )

    content2 = ActionResponseContent(
        function="test",
        arguments={"a": 1},
        output="result",
        action_request_id="req1",
    )

    content3 = ActionResponseContent(
        function="different",
        arguments={"a": 1},
        output="result",
        action_request_id="req1",
    )

    # Same values should be equal
    assert content1 == content2

    # Different values should not be equal
    assert content1 != content3


def test_action_response_multiple_instances_independence():
    """Test that multiple ActionResponse instances are independent."""
    response1 = ActionResponse(content={"function": "func1", "arguments": {"x": 1}, "output": 1})

    response2 = ActionResponse(content={"function": "func2", "arguments": {"x": 2}, "output": 2})

    # Modify one instance
    response1.content.arguments["x"] = 999

    # Verify other instance is not affected
    assert response2.content.arguments["x"] == 2
    assert response1.content.function == "func1"
    assert response2.content.function == "func2"


def test_action_response_content_rendered_is_always_string():
    """Test that rendered property always returns a string, even for edge cases."""
    test_cases = [
        ActionResponseContent(function="", arguments={}, output=None),
        ActionResponseContent(function="f", arguments={}, output=""),
        ActionResponseContent(function="f", arguments={}, output=0),
        ActionResponseContent(function="f", arguments={}, output=False),
        ActionResponseContent(function="f", arguments={}, output=[]),
        ActionResponseContent(function="f", arguments={}, output={}),
    ]

    for content in test_cases:
        rendered = content.rendered
        assert isinstance(rendered, str)
        assert len(rendered) > 0  # Should not be empty string
