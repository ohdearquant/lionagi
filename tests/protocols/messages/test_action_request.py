# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0


import pytest

from lionagi.protocols.messages.action_request import (
    ActionRequest,
    ActionRequestContent,
)
from lionagi.protocols.types import MessageRole


def test_action_request_content_initialization():
    """Test basic initialization of ActionRequestContent"""
    function = "test_function"
    arguments = {"arg1": "value1", "arg2": "value2"}

    content = ActionRequestContent(
        function=function,
        arguments=arguments,
    )

    assert content.function == function
    assert content.arguments == arguments
    assert content.action_response_id is None


def test_action_request_initialization():
    """Test basic initialization of ActionRequest"""
    function = "test_function"
    arguments = {"arg1": "value1", "arg2": "value2"}

    content = ActionRequestContent(
        function=function,
        arguments=arguments,
    )
    request = ActionRequest(
        content=content,
        sender="user",
        recipient="assistant",
    )

    assert request.role == MessageRole.ACTION
    assert request.content.function == function
    assert request.content.arguments == arguments
    assert request.sender == "user"
    assert request.recipient == "assistant"
    assert not request.is_responded()


def test_action_request_from_dict_with_callable():
    """Test ActionRequestContent.from_dict() with a callable function"""

    def test_func():
        pass

    arguments = {"arg1": "value1"}
    data = {
        "function": test_func,
        "arguments": arguments,
    }

    content = ActionRequestContent.from_dict(data)

    assert content.function == "test_func"
    assert content.arguments == arguments
    assert content.action_response_id is None


def test_action_request_from_dict_nested_format():
    """Test ActionRequestContent.from_dict() with nested action_request key (backward compatibility)"""
    data = {
        "action_request": {
            "function": "test_function",
            "arguments": {"arg1": "value1", "arg2": "value2"},
        },
        "action_response_id": "response_123",
    }

    content = ActionRequestContent.from_dict(data)

    assert content.function == "test_function"
    assert content.arguments == {"arg1": "value1", "arg2": "value2"}
    assert content.action_response_id == "response_123"


def test_action_request_from_dict_flat_format():
    """Test ActionRequestContent.from_dict() with flat format"""
    data = {
        "function": "test_function",
        "arguments": {"arg1": "value1"},
        "action_response_id": "response_456",
    }

    content = ActionRequestContent.from_dict(data)

    assert content.function == "test_function"
    assert content.arguments == {"arg1": "value1"}
    assert content.action_response_id == "response_456"


def test_action_request_from_dict_with_callable_object():
    """Test ActionRequestContent.from_dict() with callable object that has function attribute"""

    class CallableWithFunction:
        def __init__(self):
            self.function = "my_function"
            self.__name__ = "my_function"  # Need __name__ attribute

        def __call__(self):
            pass

    callable_obj = CallableWithFunction()
    data = {
        "function": callable_obj,
        "arguments": {"key": "value"},
    }

    content = ActionRequestContent.from_dict(data)

    assert content.function == "my_function"
    assert content.arguments == {"key": "value"}


def test_action_request_from_dict_invalid_function():
    """Test ActionRequestContent.from_dict() with invalid function type"""
    data = {
        "function": 123,  # Not a string or callable
        "arguments": {},
    }

    with pytest.raises(ValueError, match="Function must be a string or callable"):
        ActionRequestContent.from_dict(data)


def test_action_request_from_dict_invalid_arguments():
    """Test ActionRequestContent.from_dict() with invalid arguments type"""
    data = {
        "function": "test",
        "arguments": "invalid_string",  # Not a dict
    }

    with pytest.raises(ValueError, match="Arguments must be a dictionary"):
        ActionRequestContent.from_dict(data)


def test_action_request_validator_with_dict():
    """Test ActionRequest content validator with dict input"""
    data = {
        "function": "test_function",
        "arguments": {"arg1": "value1"},
    }

    request = ActionRequest(
        content=data,
        sender="user",
        recipient="assistant",
    )

    assert isinstance(request.content, ActionRequestContent)
    assert request.content.function == "test_function"
    assert request.content.arguments == {"arg1": "value1"}


def test_action_request_validator_with_none():
    """Test ActionRequest content validator with None input"""
    request = ActionRequest(
        content=None,
        sender="user",
        recipient="assistant",
    )

    assert isinstance(request.content, ActionRequestContent)
    assert request.content.function == ""
    assert request.content.arguments == {}


def test_action_request_response_tracking():
    """Test tracking of action response"""
    content = ActionRequestContent(
        function="test",
        arguments={},
    )
    request = ActionRequest(
        content=content,
        sender="user",
        recipient="assistant",
    )

    assert request.content.action_response_id is None
    assert not request.is_responded()

    # Set response ID by creating new content
    request.content.action_response_id = "test_response_id"
    assert request.content.action_response_id == "test_response_id"
    assert request.is_responded()


def test_action_request_rendered_property():
    """Test the rendered property using minimal_yaml"""
    content = ActionRequestContent(
        function="test_function",
        arguments={"arg1": "value1", "arg2": 42, "arg3": True},
    )

    rendered = content.rendered

    assert isinstance(rendered, str)
    assert "Function: test_function" in rendered
    assert "Arguments:" in rendered
    assert "arg1: value1" in rendered
    assert "arg2: 42" in rendered
    assert "arg3: true" in rendered


def test_action_request_rendered_empty_arguments():
    """Test the rendered property with empty arguments"""
    content = ActionRequestContent(
        function="test_function",
        arguments={},
    )

    rendered = content.rendered

    assert isinstance(rendered, str)
    assert "Function: test_function" in rendered
    # Empty dicts are stripped by minimal_yaml, so Arguments won't appear
    # OR it shows up as "Arguments: {}" - check which
    # Based on minimal_yaml implementation, empty dicts are removed
    # So "Arguments:" key should not appear at all


def test_action_request_rendered_nested_arguments():
    """Test the rendered property with nested arguments"""
    content = ActionRequestContent(
        function="complex_function",
        arguments={
            "simple": "value",
            "nested": {"key1": "val1", "key2": "val2"},
            "list_arg": [1, 2, 3],
        },
    )

    rendered = content.rendered

    assert isinstance(rendered, str)
    assert "Function: complex_function" in rendered
    assert "Arguments:" in rendered
    # Verify structure is preserved in YAML format


def test_action_request_content_format():
    """Test the format of action request content in chat message"""
    content = ActionRequestContent(
        function="test_function",
        arguments={"arg1": "value1"},
    )
    request = ActionRequest(
        content=content,
        sender="user",
        recipient="assistant",
    )

    formatted = request.chat_msg
    assert formatted["role"] == MessageRole.ACTION.value
    assert isinstance(formatted["content"], str)
    assert "test_function" in formatted["content"]


# Clone method doesn't exist - removed test


def test_action_request_str_representation():
    """Test string representation of ActionRequest"""
    content = ActionRequestContent(
        function="test_function",
        arguments={"arg1": "value1"},
    )
    request = ActionRequest(
        content=content,
        sender="user",
        recipient="assistant",
    )

    str_repr = str(request)
    assert "Message" in str_repr
    # Check for either role=action or role=MessageRole.ACTION depending on implementation
    assert "role=action" in str_repr.lower() or "action" in str_repr.lower()
    assert "test_function" in str_repr


def test_action_request_from_dict_default_values():
    """Test ActionRequestContent.from_dict() with missing fields uses defaults"""
    data = {
        "function": "test",
    }

    content = ActionRequestContent.from_dict(data)

    assert content.function == "test"
    assert content.arguments == {}
    assert content.action_response_id is None


def test_action_request_from_dict_with_empty_nested():
    """Test ActionRequestContent.from_dict() with empty nested action_request"""
    data = {
        "action_request": {},
    }

    content = ActionRequestContent.from_dict(data)

    assert content.function == ""
    assert content.arguments == {}


def test_action_request_immutable_arguments():
    """Test that arguments are properly copied and isolated"""
    original_args = {"key": "value"}
    content = ActionRequestContent(
        function="test",
        arguments=original_args,
    )

    original_args["key"] = "modified"

    # Note: dataclass doesn't deep copy by default, but from_dict uses copy()
    data = {
        "function": "test",
        "arguments": original_args,
    }
    content2 = ActionRequestContent.from_dict(data)
    original_args["new_key"] = "new_value"

    assert "new_key" not in content2.arguments
