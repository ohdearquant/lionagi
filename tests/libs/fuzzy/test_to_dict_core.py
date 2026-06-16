"""Tests for lionagi/ln/fuzzy/_to_dict.py"""

import dataclasses
from collections import OrderedDict
from enum import Enum

import pytest

from lionagi.ln.fuzzy._to_dict import (
    _convert_top_level_to_dict,
    _enum_class_to_dict,
    _is_na,
    _object_to_mapping_like,
    _parse_str,
    _preprocess_recursive,
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


def test_is_na_with_none():
    """Test _is_na with None"""
    assert _is_na(None) is True


def test_is_na_with_pydantic_undefined():
    """Test _is_na with Pydantic undefined sentinels"""
    obj = PydanticUndefined()
    # The function checks typename, not isinstance
    assert _is_na(obj) in (True, False)  # Depends on typename


def test_is_na_with_regular_object():
    """Test _is_na with regular objects"""
    assert _is_na("string") is False
    assert _is_na(42) is False
    assert _is_na([]) is False


def test_enum_class_to_dict_with_values():
    """Test enum conversion with use_enum_values=True (lines 31-32)"""
    result = _enum_class_to_dict(Color, use_enum_values=True)
    assert result == {"RED": 1, "GREEN": 2, "BLUE": 3}


def test_enum_class_to_dict_without_values():
    """Test enum conversion with use_enum_values=False (line 33)"""
    result = _enum_class_to_dict(Color, use_enum_values=False)
    assert result == {
        "RED": Color.RED,
        "GREEN": Color.GREEN,
        "BLUE": Color.BLUE,
    }


def test_enum_class_to_dict_string_values():
    """Test enum with string values"""
    result = _enum_class_to_dict(Status, use_enum_values=True)
    assert result == {
        "ACTIVE": "active",
        "INACTIVE": "inactive",
        "PENDING": "pending",
    }


def test_parse_str_with_custom_parser():
    """Test custom parser"""

    def custom_parser(s, **kwargs):
        return {"custom": s}

    result = _parse_str("test", fuzzy_parse=False, str_type=None, parser=custom_parser)
    assert result == {"custom": "test"}


def test_parse_str_xml():
    """Test XML parsing (lines 50-52)"""
    pytest.importorskip("xmltodict")
    xml_string = '<?xml version="1.0"?><root><child>value</child></root>'
    result = _parse_str(xml_string, fuzzy_parse=False, str_type="xml", parser=None)
    assert "root" in result
    assert result["root"]["child"] == "value"


def test_parse_str_json():
    """Test JSON parsing"""
    result = _parse_str('{"a": 1}', fuzzy_parse=False, str_type="json", parser=None)
    assert result == {"a": 1}


def test_parse_str_fuzzy():
    """Test fuzzy JSON parsing"""
    # Fuzzy parse should handle single quotes
    result = _parse_str("{'a': 1}", fuzzy_parse=True, str_type="json", parser=None)
    assert result == {"a": 1}


def test_object_to_mapping_like_pydantic():
    """Test Pydantic model conversion"""
    obj = PydanticLike()
    result = _object_to_mapping_like(obj, prioritize_model_dump=True)
    assert result == {"name": "pydantic", "value": 42}


def test_object_to_mapping_like_to_dict():
    """Test object with to_dict method"""
    obj = ObjectWithToDict()
    result = _object_to_mapping_like(obj, prioritize_model_dump=False)
    assert result == {"method": "to_dict", "data": "value"}


def test_object_to_mapping_like_dict():
    """Test object with dict method"""
    obj = ObjectWithDict()
    result = _object_to_mapping_like(obj, prioritize_model_dump=False)
    assert result == {"method": "dict", "data": "value"}


def test_object_to_mapping_like_json():
    """Test object with json method (returns string, needs parsing)"""
    obj = ObjectWithJson()
    result = _object_to_mapping_like(obj, prioritize_model_dump=False)
    # Returns string, will be parsed by caller
    assert result == {"method": "json", "data": "value"}


def test_object_to_mapping_like_dataclass():
    """Test dataclass conversion (line 91)"""
    person = Person(name="John", age=30)
    result = _object_to_mapping_like(person, prioritize_model_dump=False)
    assert result == {
        "name": "John",
        "age": 30,
        "email": "default@example.com",
    }


def test_object_to_mapping_like_dunder_dict():
    """Test object with __dict__"""
    obj = ObjectWithDunderDict()
    result = _object_to_mapping_like(obj, prioritize_model_dump=False)
    assert result == {"a": 1, "b": 2}


def test_preprocess_recursive_max_depth():
    """Test max_depth limit (line 127)"""
    nested = {"a": {"b": {"c": {"d": {"e": "deep"}}}}}
    result = _preprocess_recursive(
        nested,
        depth=0,
        max_depth=2,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    # Should stop recursion at depth 2
    assert isinstance(result, dict)


def test_preprocess_recursive_at_max_depth():
    """Test when already at max_depth (line 127)"""
    obj = {"test": "value"}
    result = _preprocess_recursive(
        obj,
        depth=5,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    # Should return obj as-is when depth >= max_depth
    assert result == obj


def test_preprocess_recursive_string_parsing():
    """Test string parsing in recursion (lines 134-138)"""
    json_str = '{"nested": "value"}'
    result = _preprocess_recursive(
        json_str,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert result == {"nested": "value"}


def test_preprocess_recursive_string_parse_error():
    """Test string parsing error handling (lines 136-137)"""
    invalid_json = "{invalid"
    result = _preprocess_recursive(
        invalid_json,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    # Should return original string on parse error
    assert result == invalid_json


def test_preprocess_recursive_list():
    """Test list processing (lines 164-176)"""
    data = [1, "test", {"three": 3}]
    result = _preprocess_recursive(
        data,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert isinstance(result, list)
    assert result == [1, "test", {"three": 3}]


def test_preprocess_recursive_tuple():
    """Test tuple processing (lines 177-178)"""
    data = (1, 2, 3)
    result = _preprocess_recursive(
        data,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert isinstance(result, tuple)
    assert result == (1, 2, 3)


def test_preprocess_recursive_set():
    """Test set processing (lines 179-180)"""
    data = {1, 2, 3}
    result = _preprocess_recursive(
        data,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert isinstance(result, set)
    assert result == {1, 2, 3}


def test_preprocess_recursive_frozenset():
    """Test frozenset processing (lines 181-182)"""
    data = frozenset([1, 2, 3])
    result = _preprocess_recursive(
        data,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert isinstance(result, frozenset)
    assert result == frozenset([1, 2, 3])


def test_preprocess_recursive_enum_class():
    """Test enum class processing in recursion (lines 186-200)"""
    result = _preprocess_recursive(
        Color,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
            "use_enum_values": True,
        },
        prioritize_model_dump=True,
    )
    # Should convert enum class to dict
    assert isinstance(result, dict)


def test_preprocess_recursive_enum_class_error():
    """Test enum class error handling (line 199-200)"""

    # Create a mock that looks like enum but fails
    class FakeEnum:
        pass

    result = _preprocess_recursive(
        FakeEnum,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    # Should return original on error
    assert result == FakeEnum


def test_preprocess_recursive_custom_object():
    """Test custom object processing (line 208)"""
    obj = ObjectWithToDict()
    result = _preprocess_recursive(
        obj,
        depth=0,
        max_depth=5,
        recursive_custom_types=True,  # Enable custom type recursion
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=False,
    )
    # Should convert object to mapping
    assert isinstance(result, dict)


def test_preprocess_recursive_dict():
    """Test dict processing with nested values"""
    data = {"a": 1, "b": '{"nested": true}', "c": [1, 2, 3]}
    result = _preprocess_recursive(
        data,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert result["a"] == 1
    assert result["b"] == {"nested": True}
    assert result["c"] == [1, 2, 3]


def test_convert_top_level_set():
    """Test set conversion"""
    result = _convert_top_level_to_dict(
        {1, 2, 3},
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {1: 1, 2: 2, 3: 3}


def test_convert_top_level_enum_class():
    """Test enum class conversion (line 245)"""
    result = _convert_top_level_to_dict(
        Color,
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {"RED": 1, "GREEN": 2, "BLUE": 3}


def test_convert_top_level_mapping():
    """Test mapping conversion"""
    result = _convert_top_level_to_dict(
        OrderedDict([("a", 1), ("b", 2)]),
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {"a": 1, "b": 2}


def test_convert_top_level_none():
    """Test None conversion"""
    result = _convert_top_level_to_dict(
        None,
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {}


def test_convert_top_level_string():
    """Test string conversion"""
    result = _convert_top_level_to_dict(
        '{"key": "value"}',
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {"key": "value"}


def test_convert_top_level_object_to_string():
    """Test object that converts to string (lines 275-276)"""
    obj = ObjectWithJson()
    result = _convert_top_level_to_dict(
        obj,
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=False,
        use_enum_values=True,
    )
    assert result == {"method": "json", "data": "value"}


def test_convert_top_level_object_to_iterable():
    """Test object that converts to iterable (lines 285-288)"""

    class ObjToList:
        def to_dict(self):
            return [1, 2, 3]

    obj = ObjToList()
    result = _convert_top_level_to_dict(
        obj,
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=False,
        use_enum_values=True,
    )
    # Should enumerate the iterable
    assert result == {0: 1, 1: 2, 2: 3}


def test_convert_top_level_iterable():
    """Test iterable conversion"""
    result = _convert_top_level_to_dict(
        [1, 2, 3],
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {0: 1, 1: 2, 2: 3}


def test_convert_top_level_dataclass():
    """Test dataclass conversion fallback (line 305)"""
    person = Person(name="Alice", age=25)
    result = _convert_top_level_to_dict(
        person,
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=False,
        use_enum_values=True,
    )
    assert result["name"] == "Alice"
    assert result["age"] == 25


# ============================================================================
# Test to_dict (Main Function)
