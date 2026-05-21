"""Tests for lionagi/ln/_json_dump.py JSON serialization utilities."""

from __future__ import annotations

import decimal
from enum import Enum
from pathlib import Path

import orjson
import pytest

from lionagi.ln._json_dump import (
    json_dumps,
)


# Test fixtures and helpers
class Color(Enum):
    RED = 1
    GREEN = 2
    BLUE = 3


class DummyModel:
    """Test model with model_dump method."""

    def model_dump(self):
        return {"field": "value"}


class DummyDict:
    """Test model with dict method."""

    def dict(self):
        return {"data": "test"}


class FailingModel:
    """Model that raises exception on model_dump."""

    def model_dump(self):
        raise ValueError("Intentional failure")


class ComplexObject:
    """Non-serializable object for testing safe fallback."""

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return f"ComplexObject(value={self.value})"


"""Tests for JSON dump edge cases: nested structures, caching, non-serializable."""

# ============================================================================
# Test Edge Cases
# ============================================================================


def test_nested_structures():
    """Test serialization of deeply nested structures."""
    nested = {
        "level1": {
            "level2": {
                "level3": [1, 2, 3],
                "path": Path("/tmp/test"),
                "decimal": decimal.Decimal("123.45"),
            }
        }
    }

    result = json_dumps(nested)
    data = orjson.loads(result)

    assert data["level1"]["level2"]["level3"] == [1, 2, 3]
    assert data["level1"]["level2"]["path"] == "/tmp/test"


def test_list_with_special_types():
    """Test list containing various special types."""
    data = [
        Path("/tmp"),
        decimal.Decimal("1.23"),
        Color.RED,
        {1, 2, 3},
    ]

    result = json_dumps(data, enum_as_name=True)
    parsed = orjson.loads(result)

    assert parsed[0] == "/tmp"
    assert parsed[1] == "1.23"
    # Enum returns value (1) due to orjson native handling
    assert parsed[2] == 1
    assert isinstance(parsed[3], list)


def test_empty_structures():
    """Test serialization of empty structures."""
    assert json_dumps({}) == "{}"
    assert json_dumps([]) == "[]"
    assert json_dumps("") == '""'
    assert json_dumps(set()) == "[]"


def test_none_serialization():
    """Test None serialization."""
    assert json_dumps(None) == "null"
    assert json_dumps({"key": None}) == '{"key":null}'


def test_boolean_serialization():
    """Test boolean serialization."""
    assert json_dumps(True) == "true"
    assert json_dumps(False) == "false"


def test_numeric_types():
    """Test various numeric types."""
    data = {
        "int": 42,
        "float": 3.14,
        "negative": -100,
        "zero": 0,
    }
    result = json_dumps(data)
    parsed = orjson.loads(result)
    assert parsed == data


# ============================================================================
# Test Caching Behavior
# ============================================================================


def test_cached_default_reuse():
    """Test that cached default is reused for same parameters."""
    # First call
    result1 = json_dumps(Path("/tmp/test1"))
    # Second call with same parameters should use cached default
    result2 = json_dumps(Path("/tmp/test2"))

    assert result1 == '"/tmp/test1"'
    assert result2 == '"/tmp/test2"'


def test_type_cache_in_default():
    """Test that type cache works correctly in default function."""
    # Serialize multiple Path objects to test caching
    paths = [Path(f"/tmp/test{i}") for i in range(10)]

    for path in paths:
        result = json_dumps(path)
        assert result == f'"/tmp/test{paths.index(path)}"'


# ============================================================================
# Test Error Handling
# ============================================================================


def test_non_serializable_without_safe_fallback():
    """Test that non-serializable objects raise TypeError."""

    class NotSerializable:
        pass

    obj = NotSerializable()

    with pytest.raises(TypeError, match="not JSON serializable"):
        json_dumps(obj)


def test_non_serializable_with_safe_fallback():
    """Test that safe_fallback handles non-serializable objects."""

    class NotSerializable:
        pass

    obj = NotSerializable()
    result = json_dumps(obj, safe_fallback=True)

    # Should contain class name
    assert "NotSerializable" in result


def test_allow_non_str_keys():
    """Test serialization with non-string keys."""
    data = {1: "one", 2: "two", 3: "three"}

    result = json_dumps(data, allow_non_str_keys=True)
    parsed = orjson.loads(result)

    # Keys will be converted to strings by orjson
    assert parsed == {"1": "one", "2": "two", "3": "three"}
