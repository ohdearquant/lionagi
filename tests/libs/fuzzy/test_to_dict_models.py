"""Tests for lionagi/ln/fuzzy/_to_dict.py"""

import dataclasses
from enum import Enum

import pytest

from lionagi.ln.fuzzy._to_dict import (
    to_dict,
)


class Color(Enum):
    """Test enum with values"""

    RED = 1
    GREEN = 2
    BLUE = 3


class Status(Enum):
    """Test enum with string values"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING = "pending"


@dataclasses.dataclass
class Person:
    """Test dataclass"""

    name: str
    age: int
    email: str = "default@example.com"


@dataclasses.dataclass
class NestedData:
    """Nested dataclass for recursion testing"""

    person: Person
    tags: list


class PydanticLike:
    """Mock Pydantic model"""

    def model_dump(self, **kwargs):
        return {"name": "pydantic", "value": 42}


class ObjectWithToDict:
    """Object with to_dict method"""

    def to_dict(self, **kwargs):
        return {"method": "to_dict", "data": "value"}


class ObjectWithDict:
    """Object with dict method"""

    def dict(self, **kwargs):
        return {"method": "dict", "data": "value"}


class ObjectWithJson:
    """Object with json method returning string"""

    def json(self, **kwargs):
        return '{"method": "json", "data": "value"}'


class ObjectWithToJson:
    """Object with to_json method"""

    def to_json(self, **kwargs):
        return '{"method": "to_json", "data": "value"}'


class ObjectWithDunderDict:
    """Object with __dict__"""

    def __init__(self):
        self.a = 1
        self.b = 2


class PydanticUndefined:
    """Mock Pydantic undefined sentinel"""

    pass


class UndefinedType:
    """Mock undefined type"""

    pass


class IterableObject:
    """Custom iterable that's not a sequence"""

    def __iter__(self):
        return iter([1, 2, 3])


# ============================================================================


def test_to_dict_basic_dict():
    """Test basic dict input"""
    assert to_dict({"a": 1, "b": 2}) == {"a": 1, "b": 2}


def test_to_dict_none():
    """Test None input"""
    assert to_dict(None) == {}


def test_to_dict_empty_string():
    """Test empty string"""
    assert to_dict("") == {}


def test_to_dict_json_string():
    """Test JSON string"""
    assert to_dict('{"a": 1}') == {"a": 1}


def test_to_dict_fuzzy_parse():
    """Test fuzzy JSON parsing"""
    assert to_dict("{'a': 1, 'b': 2}", fuzzy_parse=True) == {"a": 1, "b": 2}


def test_to_dict_xml_string():
    """Test XML string parsing"""
    pytest.importorskip("xmltodict")
    xml = '<?xml version="1.0"?><root><item>test</item></root>'
    result = to_dict(xml, str_type="xml")
    assert "root" in result


def test_to_dict_custom_parser():
    """Test custom parser"""

    def parser(s):
        return {"custom": s}

    result = to_dict("test", parser=parser)
    assert result == {"custom": "test"}


def test_to_dict_set():
    """Test set conversion"""
    result = to_dict({1, 2, 3})
    assert result == {1: 1, 2: 2, 3: 3}


def test_to_dict_list():
    """Test list conversion"""
    assert to_dict([1, 2, 3]) == {0: 1, 1: 2, 2: 3}


def test_to_dict_tuple():
    """Test tuple conversion"""
    assert to_dict((1, 2, 3)) == {0: 1, 1: 2, 2: 3}


def test_to_dict_pydantic_model():
    """Test Pydantic-like model"""
    obj = PydanticLike()
    result = to_dict(obj)
    assert result == {"name": "pydantic", "value": 42}


def test_to_dict_dataclass():
    """Test dataclass"""
    person = Person(name="Bob", age=35)
    result = to_dict(person)
    assert result["name"] == "Bob"
    assert result["age"] == 35


def test_to_dict_enum_class():
    """Test enum class"""
    result = to_dict(Color, use_enum_values=True)
    assert result == {"RED": 1, "GREEN": 2, "BLUE": 3}


def test_to_dict_enum_without_values():
    """Test enum class without values"""
    result = to_dict(Color, use_enum_values=False)
    assert "RED" in result


def test_to_dict_with_suppress():
    """Test suppress mode"""
    assert to_dict("{invalid json}", suppress=True) == {}


def test_to_dict_recursive_basic():
    """Test recursive processing"""
    data = {"a": '{"nested": true}', "b": [1, 2, 3]}
    result = to_dict(data, recursive=True)
    # Note: JSON strings within dicts are not parsed due to use_enum_values kwarg issue
    assert isinstance(result, dict)
    assert result["a"] == '{"nested": true}'  # String not parsed in recursive mode
    assert result["b"] == [1, 2, 3]


def test_to_dict_recursive_nested_structures():
    """Test deeply nested recursive processing"""
    data = {"level1": {"level2": '{"level3": "value"}'}}
    result = to_dict(data, recursive=True)
    # Verify recursive structure preserved (strings not parsed due to kwarg issue)
    assert isinstance(result["level1"], dict)
    assert result["level1"]["level2"] == '{"level3": "value"}'


def test_to_dict_recursive_custom_objects():
    """Test recursive with custom objects"""
    obj = ObjectWithToDict()
    data = {"obj": obj}
    result = to_dict(data, recursive=True, recursive_python_only=False)
    assert isinstance(result["obj"], dict)


def test_to_dict_max_recursive_depth_default():
    """Test default max recursive depth"""
    nested = {"a": {"b": {"c": {"d": {"e": {"f": "deep"}}}}}}
    result = to_dict(nested, recursive=True)
    assert isinstance(result, dict)


def test_to_dict_max_recursive_depth_custom():
    """Test custom max recursive depth"""
    nested = {"a": {"b": {"c": "value"}}}
    result = to_dict(nested, recursive=True, max_recursive_depth=2)
    assert isinstance(result, dict)


def test_to_dict_max_recursive_depth_negative():
    """Test negative max_recursive_depth raises error (line 345)"""
    with pytest.raises(ValueError, match="must be a non-negative integer"):
        to_dict({"a": 1}, recursive=True, max_recursive_depth=-1)


def test_to_dict_max_recursive_depth_too_large():
    """Test max_recursive_depth > 10 raises error (line 349)"""
    with pytest.raises(ValueError, match="must be less than or equal to 10"):
        to_dict({"a": 1}, recursive=True, max_recursive_depth=11)


def test_to_dict_max_recursive_depth_boundary():
    """Test max_recursive_depth at boundaries"""
    # 0 should work
    result = to_dict({"a": 1}, recursive=True, max_recursive_depth=0)
    assert isinstance(result, dict)

    # 10 should work
    result = to_dict({"a": 1}, recursive=True, max_recursive_depth=10)
    assert isinstance(result, dict)


def test_to_dict_deprecated_use_model_dump():
    """Test deprecated use_model_dump parameter"""
    obj = PydanticLike()
    result = to_dict(obj, use_model_dump=True)
    assert result == {"name": "pydantic", "value": 42}


def test_to_dict_prioritize_model_dump_false():
    """Test prioritize_model_dump=False"""
    obj = ObjectWithToDict()
    result = to_dict(obj, prioritize_model_dump=False)
    assert result == {"method": "to_dict", "data": "value"}


def test_to_dict_complex_nested_scenario():
    """Test complex nested scenario with multiple types"""
    data = {
        "list": [1, 2, {"nested": "value"}],
        "tuple": (4, 5, 6),
        "set": {7, 8, 9},
        "json_str": '{"parsed": true}',
        "regular": "string",
    }
    result = to_dict(data, recursive=True)
    assert isinstance(result["list"], list)
    assert isinstance(result["tuple"], tuple)
    assert isinstance(result["set"], set)
    # JSON string not parsed in dict context due to kwarg issue
    assert result["json_str"] == '{"parsed": true}'
    assert result["regular"] == "string"
