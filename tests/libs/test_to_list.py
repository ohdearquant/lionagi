# tests/libs/test_to_list.py
from collections.abc import Mapping
from enum import Enum

import pytest
from pydantic import BaseModel

from lionagi.ln._to_list import ToListParams, to_list


# Enum for testing
class Color(Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


class Status(Enum):
    ACTIVE = 1
    INACTIVE = 0
    PENDING = 2


# Pydantic model for testing
class SampleModel(BaseModel):
    name: str
    value: int


# Custom mapping for testing
class CustomMapping(Mapping):
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


class TestToListBasic:
    """Test basic to_list functionality."""

    def test_none_input(self):
        assert to_list(None) == []

    def test_list_input(self):
        assert to_list([1, 2, 3]) == [1, 2, 3]

    def test_string_input(self):
        assert to_list("hello") == ["hello"]

    def test_int_input(self):
        assert to_list(42) == [42]

    def test_tuple_input(self):
        assert to_list((1, 2, 3)) == [1, 2, 3]

    def test_set_input(self):
        result = to_list({1, 2, 3})
        assert len(result) == 3
        assert set(result) == {1, 2, 3}

    def test_generator_input(self):
        gen = (x for x in range(3))
        assert to_list(gen) == [0, 1, 2]


class TestToListEnums:
    """Test enum handling."""

    def test_enum_instance(self):
        assert to_list(Color.RED) == [Color.RED]

    def test_enum_class_without_values(self):
        result = to_list(Color, use_values=False)
        assert len(result) == 3
        assert Color.RED in result
        assert Color.GREEN in result
        assert Color.BLUE in result

    def test_enum_class_with_values(self):
        """Test lines 114-115: Enum class with use_values=True."""
        result = to_list(Color, use_values=True)
        assert result == ["red", "green", "blue"]

    def test_enum_class_with_int_values(self):
        """Test lines 114-115: Enum class with integer values."""
        result = to_list(Status, use_values=True)
        assert set(result) == {1, 0, 2}


class TestToListStringsAndBytes:
    """Test string and byte-like types."""

    def test_string_use_values_false(self):
        assert to_list("test", use_values=False) == ["test"]

    def test_string_use_values_true(self):
        result = to_list("abc", use_values=True)
        assert result == ["a", "b", "c"]

    def test_bytes_use_values_false(self):
        assert to_list(b"test", use_values=False) == [b"test"]

    def test_bytes_use_values_true(self):
        result = to_list(b"abc", use_values=True)
        assert result == [97, 98, 99]

    def test_bytearray_use_values_false(self):
        assert to_list(bytearray(b"test"), use_values=False) == [bytearray(b"test")]


class TestToListMappings:
    """Test mapping and dict handling."""

    def test_dict_use_values_false(self):
        d = {"a": 1, "b": 2}
        assert to_list(d, use_values=False) == [d]

    def test_dict_use_values_true(self):
        d = {"a": 1, "b": 2, "c": 3}
        result = to_list(d, use_values=True)
        assert set(result) == {1, 2, 3}

    def test_custom_mapping_use_values_true(self):
        m = CustomMapping({"x": 10, "y": 20})
        result = to_list(m, use_values=True)
        assert set(result) == {10, 20}

    def test_pydantic_model(self):
        model = SampleModel(name="test", value=42)
        assert to_list(model) == [model]


class TestToListFlatten:
    """Test flattening functionality."""

    def test_flatten_nested_list(self):
        assert to_list([[1, 2], [3, 4]], flatten=True) == [1, 2, 3, 4]

    def test_flatten_deeply_nested(self):
        assert to_list([[[1]], [[2]], [[3]]], flatten=True) == [1, 2, 3]

    def test_flatten_mixed_types(self):
        result = to_list([[1, 2], 3, [4, [5, 6]]], flatten=True)
        assert result == [1, 2, 3, 4, 5, 6]

    def test_no_flatten(self):
        result = to_list([[1, 2], [3, 4]], flatten=False)
        assert result == [[1, 2], [3, 4]]

    def test_flatten_tuple_set_false(self):
        """By default, tuples/sets are not flattened."""
        result = to_list([(1, 2), (3, 4)], flatten=True, flatten_tuple_set=False)
        assert result == [(1, 2), (3, 4)]

    def test_flatten_tuple_set_true(self):
        """With flatten_tuple_set=True, tuples/sets are flattened."""
        result = to_list([(1, 2), (3, 4)], flatten=True, flatten_tuple_set=True)
        assert result == [1, 2, 3, 4]


class TestToListDropna:
    """Test dropna functionality."""

    def test_dropna_none(self):
        assert to_list([1, None, 2, None, 3], dropna=True) == [1, 2, 3]

    def test_dropna_false(self):
        assert to_list([1, None, 2], dropna=False) == [1, None, 2]

    def test_dropna_nested(self):
        result = to_list([[1, None], [2, None, 3]], flatten=True, dropna=True)
        assert result == [1, 2, 3]


class TestToListUnique:
    """Test unique functionality."""

    def test_unique_requires_flatten(self):
        with pytest.raises(ValueError, match="unique=True requires flatten=True"):
            to_list([1, 2, 3], unique=True, flatten=False)

    def test_unique_simple(self):
        result = to_list([1, 2, 1, 3, 2], flatten=True, unique=True)
        assert result == [1, 2, 3]

    def test_unique_nested(self):
        result = to_list([[1, 2], [2, 3], [1, 4]], flatten=True, unique=True)
        assert result == [1, 2, 3, 4]

    def test_unique_with_strings(self):
        result = to_list([["a", "b"], ["b", "c"]], flatten=True, unique=True)
        assert result == ["a", "b", "c"]

    def test_unique_preserves_order(self):
        result = to_list([[3, 2], [1, 2]], flatten=True, unique=True)
        assert result == [3, 2, 1]

    def test_unique_with_unhashable_dict(self):
        """Test lines 158-165: Hash-based fallback for unhashable items."""
        # Mix hashable and unhashable items to trigger fallback
        data = [[1, 2], [{"a": 1}, 3], [{"a": 1}, 4]]
        result = to_list(data, flatten=True, unique=True)

        # Should have: 1, 2, one dict {"a": 1}, 3, 4
        assert 1 in result
        assert 2 in result
        assert 3 in result
        assert 4 in result

        # Count dicts
        dicts = [x for x in result if isinstance(x, dict)]
        assert len(dicts) == 1
        assert dicts[0] == {"a": 1}

    def test_unique_with_multiple_dicts(self):
        """Test lines 158-165: Multiple different unhashable items."""
        data = [[{"a": 1}, {"b": 2}], [{"a": 1}, {"c": 3}]]
        result = to_list(data, flatten=True, unique=True)

        # Should have 3 unique dicts
        dicts = [x for x in result if isinstance(x, dict)]
        assert len(dicts) == 3
        assert {"a": 1} in dicts
        assert {"b": 2} in dicts
        assert {"c": 3} in dicts

    def test_unique_with_pydantic_models(self):
        """Test lines 158-165: Pydantic models in unique processing."""
        model1 = SampleModel(name="test1", value=1)
        model2 = SampleModel(name="test2", value=2)
        model3 = SampleModel(name="test1", value=1)  # Duplicate of model1

        data = [[model1, model2], [model3]]
        result = to_list(data, flatten=True, unique=True)

        # Should have 2 unique models (model1/model3 are same hash)
        assert len(result) == 2

    def test_unique_mixed_hashable_unhashable(self):
        """Test lines 158-165: Mixed hashable and unhashable to trigger fallback path."""
        # Start with hashable, then introduce unhashable
        data = [[1, 2, 3], [{"x": 1}, 2], [{"y": 2}, 4]]
        result = to_list(data, flatten=True, unique=True)

        # Should have: 1, 2, 3, {"x": 1}, {"y": 2}, 4
        nums = [x for x in result if isinstance(x, int)]
        dicts = [x for x in result if isinstance(x, dict)]

        assert set(nums) == {1, 2, 3, 4}
        assert len(dicts) == 2

    def test_unique_unhashable_non_mapping_error(self):
        """Test line 180: ValueError for unhashable non-mapping types."""

        # Create a custom unhashable type that's not a mapping
        class UnhashableType:
            __hash__ = None  # Explicitly unhashable

            def __init__(self, value):
                self.value = value

        # Mix hashable with custom unhashable type to trigger TypeError path
        obj1 = UnhashableType(1)
        obj2 = UnhashableType(2)

        data = [[1, 2], [obj1, 3], [obj2]]

        # Should raise ValueError because UnhashableType is not in _MAP_LIKE
        with pytest.raises(ValueError, match="Unhashable type encountered"):
            to_list(data, flatten=True, unique=True)

    def test_unique_with_custom_hash_object(self):
        """Test lines 158-165: Force hash-based approach with custom hash.

        This test attempts to cover the else branch (lines 158-165) by creating
        an object with a custom __hash__ that initially works, then using dict
        operations that trigger the hash-based path.
        """

        # Create objects that have custom __hash__ but may behave differently
        class HashableWithAttr:
            def __init__(self, value):
                self.value = value
                self._hash = hash(value)

            def __hash__(self):
                return self._hash

            def __eq__(self, other):
                return isinstance(other, HashableWithAttr) and self.value == other.value

        # Test with these objects
        obj1 = HashableWithAttr(1)
        obj2 = HashableWithAttr(2)
        obj3 = HashableWithAttr(1)  # Duplicate

        data = [[obj1, obj2], [obj3]]
        result = to_list(data, flatten=True, unique=True)

        # Should have 2 unique objects (obj1 and obj3 have same hash/value)
        assert len(result) == 2


class TestToListCombinations:
    """Test combinations of options."""

    def test_flatten_dropna_unique(self):
        data = [[1, None, 2], [2, 3, None], [3, 4]]
        result = to_list(data, flatten=True, dropna=True, unique=True)
        assert result == [1, 2, 3, 4]

    def test_use_values_with_dict(self):
        # use_values works on the top-level input, not nested dicts in a list
        # When top-level is a dict with use_values=True, extract values
        d = {"a": 1, "b": 2, "c": 3}
        result = to_list(d, use_values=True)
        assert set(result) == {1, 2, 3}

    def test_complex_nested_structure(self):
        data = [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]
        result = to_list(data, flatten=True)
        assert result == [1, 2, 3, 4, 5, 6, 7, 8]


class TestToListParams:
    """Test ToListParams dataclass."""

    def test_to_list_params_creation(self):
        params = ToListParams(
            flatten=True,
            dropna=True,
            unique=True,
            use_values=False,
            flatten_tuple_set=False,
        )
        assert params.flatten is True
        assert params.dropna is True
        assert params.unique is True

    def test_to_list_params_call(self):
        """__call__ delegates to to_list using the configured params."""
        params = ToListParams(
            flatten=True,
            dropna=True,
            unique=False,
            use_values=False,
            flatten_tuple_set=False,
        )

        data = [[1, None, 2], [3, None]]
        # flatten -> [1, None, 2, 3, None]; dropna -> [1, 2, 3]
        assert params(data) == [1, 2, 3]

        # call-time kwargs override the configured params
        assert params(data, dropna=False) == [1, None, 2, 3, None]

    def test_to_list_params_to_dict(self):
        """Test to_dict method works correctly."""
        params = ToListParams(
            flatten=True,
            dropna=False,
            unique=False,
            use_values=True,
            flatten_tuple_set=False,
        )

        d = params.to_dict()
        assert d["flatten"] is True
        assert d["dropna"] is False
        assert d["use_values"] is True


class TestEdgeCases:
    """Test edge cases and special scenarios."""

    def test_empty_list(self):
        assert to_list([]) == []

    def test_nested_empty_lists(self):
        assert to_list([[], []], flatten=True) == []

    def test_single_element(self):
        assert to_list([1]) == [1]

    def test_frozenset(self):
        result = to_list(frozenset([1, 2, 3]))
        assert set(result) == {1, 2, 3}

    def test_range_object(self):
        assert to_list(range(5)) == [0, 1, 2, 3, 4]

    def test_deeply_nested_with_none(self):
        data = [[[None, 1]], [[2, None]], [[None, None]]]
        result = to_list(data, flatten=True, dropna=True)
        assert result == [1, 2]

    def test_mixed_iterables(self):
        data = [[1, 2], (3, 4), {5, 6}, [7, 8]]
        result = to_list(data, flatten=True, flatten_tuple_set=True)
        assert set(result) == {1, 2, 3, 4, 5, 6, 7, 8}


def test_unique_keeps_hash_colliding_unequal_mappings():
    """A hash collision between unequal mappings is not a duplicate.

    The unique fallback hashes unhashable items; in CPython hash(-1) == hash(-2),
    so these two mappings collide structurally while being unequal.
    """
    result = to_list([{"x": -1}, {"x": -2}, {"x": -1}], flatten=True, unique=True)
    assert result == [{"x": -1}, {"x": -2}]
