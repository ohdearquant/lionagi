"""Tests for the hash_utils module."""

import pytest

from lionagi.ln import _hash as hash_utils


class TestGenerateHashableRepresentation:
    """Tests for _generate_hashable_representation."""

    def test_primitives(self):
        assert hash_utils._generate_hashable_representation(123) == 123
        assert hash_utils._generate_hashable_representation("abc") == "abc"
        assert hash_utils._generate_hashable_representation(True) is True
        assert hash_utils._generate_hashable_representation(None) is None
        assert hash_utils._generate_hashable_representation(12.34) == 12.34

    def test_list(self):
        # Covers L45
        rep = hash_utils._generate_hashable_representation([1, "a", True])
        assert rep == (hash_utils._TYPE_MARKER_LIST, (1, "a", True))
        rep_empty = hash_utils._generate_hashable_representation([])
        assert rep_empty == (hash_utils._TYPE_MARKER_LIST, tuple())
        rep_nested = hash_utils._generate_hashable_representation([1, [2, 3]])
        expected_nested_list_rep = (hash_utils._TYPE_MARKER_LIST, (2, 3))
        assert rep_nested == (
            hash_utils._TYPE_MARKER_LIST,
            (1, expected_nested_list_rep),
        )

    def test_tuple(self):
        # Covers L51
        rep = hash_utils._generate_hashable_representation((1, "a", True))
        assert rep == (hash_utils._TYPE_MARKER_TUPLE, (1, "a", True))
        rep_empty = hash_utils._generate_hashable_representation(tuple())
        assert rep_empty == (hash_utils._TYPE_MARKER_TUPLE, tuple())

    def test_dict(self):
        rep = hash_utils._generate_hashable_representation({"b": 2, "a": 1})
        # Keys are stringified and sorted: ("a",1), ("b",2)
        expected_dict_rep = (
            hash_utils._TYPE_MARKER_DICT,
            (("a", 1), ("b", 2)),
        )
        assert rep == expected_dict_rep
        rep_empty = hash_utils._generate_hashable_representation({})
        assert rep_empty == (hash_utils._TYPE_MARKER_DICT, tuple())

    def test_set_comparable_elements(self):
        rep = hash_utils._generate_hashable_representation({3, 1, 2})
        assert rep == (hash_utils._TYPE_MARKER_SET, (1, 2, 3))

    def test_set_uncomparable_elements_fallback_sort(self):
        # Covers L70-L71 (TypeError in sort, fallback sort)
        # Create a set with types that would normally cause TypeError on direct sort
        # For example, int and str.
        # The lambda key sorts by (str(type(x)), str(x))
        # str(type(1)) -> "<class 'int'>", str(1) -> "1"
        # str(type("a")) -> "<class 'str'>", str("a") -> "a"
        # "<class 'int'>" sorts before "<class 'str'>"
        mixed_set = {1, "a"}
        rep = hash_utils._generate_hashable_representation(mixed_set)
        # Expected order: 1 (as int), then "a" (as str)
        assert rep == (hash_utils._TYPE_MARKER_SET, (1, "a"))

        mixed_set_2 = {
            "b",
            2,
            True,
        }  # True will be 1 for sorting purposes with int
        # Order: True (bool, treated like int 1), 2 (int), "b" (str)
        rep2 = hash_utils._generate_hashable_representation(mixed_set_2)
        assert rep2 == (hash_utils._TYPE_MARKER_SET, (True, 2, "b"))

    def test_frozenset_comparable_elements(self):
        # Covers L59
        rep = hash_utils._generate_hashable_representation(frozenset({3, 1, 2}))
        assert rep == (hash_utils._TYPE_MARKER_FROZENSET, (1, 2, 3))

    def test_frozenset_uncomparable_elements_fallback_sort(self):
        # Covers L60-L61 (TypeError in sort, fallback sort)
        mixed_frozenset = frozenset({1, "a"})
        rep = hash_utils._generate_hashable_representation(mixed_frozenset)
        assert rep == (hash_utils._TYPE_MARKER_FROZENSET, (1, "a"))

    class CustomObjectStr:
        def __init__(self, value):
            self.value = value

        def __str__(self):
            return f"CustomStr({self.value})"

    class CustomObjectRepr:
        def __init__(self, value):
            self.value = value

        def __str__(self):  # Make str fail
            raise TypeError("str failed")

        def __repr__(self):
            return f"CustomRepr({self.value})"

    def test_other_types_str_fallback(self):
        # Covers L79
        obj = TestGenerateHashableRepresentation.CustomObjectStr("data")
        assert hash_utils._generate_hashable_representation(obj) == "CustomStr(data)"

    def test_other_types_repr_fallback(self):
        # Covers L80-L81
        obj = TestGenerateHashableRepresentation.CustomObjectRepr("data")
        assert hash_utils._generate_hashable_representation(obj) == "CustomRepr(data)"

    class CustomObjectBothFail:
        """Object that fails both str() and repr()."""

        def __init__(self, value):
            self.value = value

        def __str__(self):
            raise RuntimeError("str failed")

        def __repr__(self):
            raise RuntimeError("repr failed")

    def test_other_types_both_str_and_repr_fail(self):
        # Covers L117-119 (fallback when both str() and repr() fail)
        obj = TestGenerateHashableRepresentation.CustomObjectBothFail("data")
        result = hash_utils._generate_hashable_representation(obj)
        # Should return fallback format: "<unhashable:ClassName:id>"
        assert result.startswith("<unhashable:CustomObjectBothFail:")
        assert result.endswith(">")
        assert "CustomObjectBothFail" in result

    def test_msgspec_struct_representation(self):
        # Covers L36-39 (msgspec.Struct handling)
        import msgspec

        class MyStruct(msgspec.Struct):
            x: int
            y: str

        struct_instance = MyStruct(x=1, y="test")
        # msgspec.to_builtins converts to dict: {"x": 1, "y": "test"}
        # _generate_hashable_representation of this dict:
        # (_TYPE_MARKER_DICT, (("x",1), ("y","test")))
        # Final result: (_TYPE_MARKER_MSGSPEC, above_dict_rep)

        expected_inner_dict_rep = (
            hash_utils._TYPE_MARKER_DICT,
            (("x", 1), ("y", "test")),  # Keys sorted
        )
        expected_rep = (
            hash_utils._TYPE_MARKER_MSGSPEC,
            expected_inner_dict_rep,
        )

        assert hash_utils._generate_hashable_representation(struct_instance) == expected_rep

    def test_pydantic_model_representation(self):
        from pydantic import BaseModel

        # Trigger lazy initialization by calling hash_dict once
        hash_utils.hash_dict({})

        class MyPydanticModel(BaseModel):
            x: int
            y: str

        model_instance = MyPydanticModel(x=1, y="test")
        # model_dump() -> {"x": 1, "y": "test"}
        # _generate_hashable_representation of this dict:
        # (_TYPE_MARKER_DICT, (("x",1), ("y","test")))
        # Final result: (_TYPE_MARKER_PYDANTIC, above_dict_rep)

        expected_inner_dict_rep = (
            hash_utils._TYPE_MARKER_DICT,
            (("x", 1), ("y", "test")),  # Keys sorted
        )
        expected_rep = (
            hash_utils._TYPE_MARKER_PYDANTIC,
            expected_inner_dict_rep,
        )

        assert hash_utils._generate_hashable_representation(model_instance) == expected_rep


class TestHashDict:
    """Tests for the main hash_dict function."""

    def test_hash_primitives(self):
        assert isinstance(hash_utils.hash_dict(123), int)
        assert isinstance(hash_utils.hash_dict("abc"), int)
        assert hash_utils.hash_dict(1) == hash_utils.hash_dict(1)
        assert hash_utils.hash_dict("a") != hash_utils.hash_dict("b")

    def test_hash_dict_deterministic(self):
        d1 = {"a": 1, "b": 2}
        d2 = {"b": 2, "a": 1}  # Same content, different order
        d3 = {"a": 1, "c": 3}
        assert hash_utils.hash_dict(d1) == hash_utils.hash_dict(d2)
        assert hash_utils.hash_dict(d1) != hash_utils.hash_dict(d3)

    def test_hash_list_tuple_deterministic(self):
        l1 = [1, {"a": 10, "b": 20}, 3]
        l2 = [1, {"b": 20, "a": 10}, 3]  # Inner dict order changed
        t1 = (1, {"a": 10, "b": 20}, 3)
        t2 = (1, {"b": 20, "a": 10}, 3)

        assert hash_utils.hash_dict(l1) == hash_utils.hash_dict(l2)
        assert hash_utils.hash_dict(t1) == hash_utils.hash_dict(t2)
        assert hash_utils.hash_dict(l1) != hash_utils.hash_dict(
            t1
        )  # List and tuple should have different hashes

    def test_hash_set_frozenset_deterministic(self):
        s1 = {1, "a", (True, None)}
        s2 = {"a", (True, None), 1}  # Different order
        fs1 = frozenset(s1)
        fs2 = frozenset(s2)

        assert hash_utils.hash_dict(s1) == hash_utils.hash_dict(s2)
        assert hash_utils.hash_dict(fs1) == hash_utils.hash_dict(fs2)
        # Hash of set and frozenset of same elements might be different due to type markers
        # but could be same if only elements are hashed. Let's check if they are different.
        # The _generate_hashable_representation adds type markers, so they will be different.
        assert hash_utils.hash_dict(s1) != hash_utils.hash_dict(fs1)

    def test_hash_pydantic_model_deterministic(self):
        from pydantic import BaseModel

        class Model(BaseModel):
            name: str
            value: int

        m1 = Model(name="test", value=1)
        m2 = Model(value=1, name="test")  # Different field order in instantiation
        m3 = Model(name="test", value=2)

        assert hash_utils.hash_dict(m1) == hash_utils.hash_dict(m2)
        assert hash_utils.hash_dict(m1) != hash_utils.hash_dict(m3)

    def test_hash_dict_strict_mode(self):
        # Covers L110
        # Create a mutable object (list) inside a dict
        data_copy_for_hash = {"a": [1, 2]}  # Ensure we hash a copy for comparison

        # Hash with strict=True
        # The variable hash_val_strict was previously assigned but not used.
        # It's removed to satisfy ruff F841.
        # The purpose of this test is to ensure that hash_dict with strict=True
        # can handle mutable objects by creating a deep copy.
        # We don't need to assert the value of the hash itself for this specific coverage.
        hash_utils.hash_dict(data_copy_for_hash, strict=True)

        # Modify the original_data AFTER the copy used for hashing would have been made by strict=True
        # This change should not affect hash_val_strict if deepcopy worked.
        # If strict=False, and original_data was passed, this modification *before* hashing would change the hash.
        # The test here is that strict=True isolates the hashing process from original object mutations.

        # To test it properly, we need to see if the hash of the original, modified object is different.
        original_data_mutated = {"a": [1, 2]}  # Start fresh for this
        hash_before_mutation_strict = hash_utils.hash_dict(original_data_mutated, strict=True)
        original_data_mutated["a"].append(3)  # Mutate it
        hash_after_mutation_strict = hash_utils.hash_dict(original_data_mutated, strict=True)

        assert hash_before_mutation_strict != hash_after_mutation_strict

        # And confirm that if strict was False, the hash would be based on the current state
        original_data_mutated_nostrict = {"a": [1, 2]}
        # hash_utils._generate_hashable_representation will process current state
        hash_nostrict_before = hash_utils.hash_dict(original_data_mutated_nostrict, strict=False)
        original_data_mutated_nostrict["a"].append(3)
        hash_nostrict_after = hash_utils.hash_dict(original_data_mutated_nostrict, strict=False)
        assert hash_nostrict_before != hash_nostrict_after

        # Check that the initial strict hash is repeatable
        data_for_repeat = {"a": [1, 2]}
        assert hash_utils.hash_dict(data_for_repeat, strict=True) == hash_utils.hash_dict(
            {"a": [1, 2]}, strict=True
        )

    def test_unhashable_representation_raises_typeerror(self):
        # Covers L116-L117
        # This requires _generate_hashable_representation to return something unhashable.
        # The current _generate_hashable_representation is designed to always return hashable tuples/primitives.
        # To test this, we would need to mock _generate_hashable_representation or make it return an unhashable type.

        original_generator = hash_utils._generate_hashable_representation
        try:
            # Mock _generate_hashable_representation to return a list (which is unhashable)
            def mock_unhashable_generator(item):
                if item == "trigger_unhashable":
                    return [
                        "this",
                        "is",
                        "a",
                        "list",
                    ]  # Lists are not hashable
                return original_generator(item)  # Fallback for other calls

            hash_utils._generate_hashable_representation = mock_unhashable_generator

            with pytest.raises(
                TypeError,
                match="The generated representation for the input data was not hashable",
            ):
                hash_utils.hash_dict("trigger_unhashable")

        finally:
            # Restore original function
            hash_utils._generate_hashable_representation = original_generator


# Add more specific test classes or functions below as needed.
