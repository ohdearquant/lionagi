from typing import Any

import pytest

from lionagi.protocols.action.tool import Tool


# Helper functions - not test cases
def example_func(x: int, y: str = "default") -> str:
    """Test function with docstring.

    Args:
        x: An integer parameter
        y: A string parameter with default

    Returns:
        A string result
    """
    return f"{x}-{y}"


def example_processor(data: dict) -> dict:
    """Test processor function."""
    return data


def example_parser(data: Any) -> str:
    """Test parser function."""
    return str(data)


# Actual test cases
def test_tool_initialization():
    """Test Tool class initialization"""
    tool = Tool(func_callable=example_func)
    assert callable(tool.func_callable)
    assert tool.func_callable == example_func
    assert tool.tool_schema is not None
    assert "function" in tool.tool_schema
    assert tool.tool_schema["function"]["name"] == "example_func"
    assert tool.preprocessor is None
    assert tool.postprocessor is None


def test_tool_with_processors():
    """Test Tool with pre/post processors"""
    tool = Tool(
        func_callable=example_func,
        preprocessor=example_processor,
        postprocessor=example_processor,
        preprocessor_kwargs={"key": "value"},
        postprocessor_kwargs={"key": "value"},
    )

    assert tool.preprocessor == example_processor
    assert tool.postprocessor == example_processor
    assert tool.preprocessor_kwargs == {"key": "value"}
    assert tool.postprocessor_kwargs == {"key": "value"}


def test_tool_validation():
    """Test Tool validation"""
    # Test non-callable function
    with pytest.raises(ValueError):
        Tool(func_callable="not_callable")

    # Test lambda function
    lambda_func = lambda x: x
    tool = Tool(func_callable=lambda_func)
    assert tool.func_callable.__name__ == "<lambda>"


def test_tool_function_name():
    """Test Tool function_name property"""
    tool = Tool(func_callable=example_func)
    assert tool.function == "example_func"


def test_tool_str_representation():
    """Test Tool string representation"""
    tool = Tool(func_callable=example_func)
    str_rep = str(tool)
    assert "id" in str_rep
    assert "created_at" in str_rep
    assert "tool_schema" in str_rep


def test_tool_tool_schemageneration():
    """Test schema generation for Tool"""
    tool = Tool(func_callable=example_func)
    schema = tool.tool_schema

    assert "function" in schema
    assert "name" in schema["function"]
    assert "description" in schema["function"]
    assert "parameters" in schema["function"]

    params = schema["function"]["parameters"]
    assert "properties" in params
    assert "x" in params["properties"]
    assert "y" in params["properties"]

    # Check parameter types
    assert params["properties"]["x"]["type"] == "number"  # int maps to number in JSON schema
    assert params["properties"]["y"]["type"] == "string"

    # Check required parameters - all parameters are required in OpenAI function format
    assert "required" in params
    assert "x" in params["required"]
    assert "y" in params["required"]  # Even parameters with defaults are marked as required


def test_tool_required_fields():
    """Test the required_fields property of Tool."""

    def func_without_defaults(a, b, c):
        return a + b + c  # type: ignore

    tool = Tool(func_callable=func_without_defaults)
    # required_fields as derived from the tool schema. For OpenAI, typically all function parameters are "required".
    # Validate that the function schema's `required` matches the property.
    assert tool.required_fields == {
        "a",
        "b",
        "c",
    }, f"Expected all params 'a', 'b', 'c' to be required, got {tool.required_fields}"


def test_tool_minimum_acceptable_fields():
    """Test the minimum_acceptable_fields property of Tool."""

    def func_mixed_args(x, y="default", *args, **kwargs):
        return x + 1  # type: ignore

    tool = Tool(func_callable=func_mixed_args)
    # Only "x" has no default in the signature (others are either defaulted or *args/**kwargs).
    assert tool.minimum_acceptable_fields == {"x"}, (
        f"Expected only 'x' to be the minimum required, got {tool.minimum_acceptable_fields}"
    )


def test_tool_strict_func_call():
    """Test that the Tool class can set and retrieve the strict_func_call flag."""

    def sample_func(a: int) -> int:
        return a + 1

    tool = Tool(func_callable=sample_func, strict_func_call=True)
    assert tool.strict_func_call is True, "strict_func_call should be set to True."

    # Change strict_func_call
    tool.strict_func_call = False
    assert tool.strict_func_call is False, "strict_func_call should now be False."


def test_tool_to_dict():
    """Test the to_dict() method of Tool."""

    def sample_func(a: int, b: int = 0) -> int:
        return a + b

    tool = Tool(func_callable=sample_func)
    serialized = tool.to_dict()

    # Basic checks on the serialized dict
    assert "function" in serialized, "Serialized dict should contain 'function' key."
    assert serialized["function"] == "sample_func", (
        f"Expected function name 'sample_func', got {serialized['function']}"
    )
    assert "id" in serialized, "The base Element fields (id, etc.) should be in the dict."
    assert "created_at" in serialized, (
        "The base Element fields (created_at, etc.) should be in the dict."
    )
    # Make sure excluded fields are not serialized
    assert "func_callable" not in serialized, "func_callable should be excluded from the dict."


def test_tool_from_dict_not_implemented():
    """Test that Tool.from_dict() raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        Tool.from_dict({"fake": "data"})
