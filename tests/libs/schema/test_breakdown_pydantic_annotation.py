"""Tests for breakdown_pydantic_annotation.py."""

from typing import Any

import pytest
from pydantic import BaseModel

from lionagi.libs.schema.breakdown_pydantic_annotation import (
    breakdown_pydantic_annotation,
)


# Test Models
class SimpleModel(BaseModel):
    name: str
    age: int
    active: bool


class NestedModel(BaseModel):
    simple: SimpleModel
    description: str


class ListModel(BaseModel):
    items: list[str]
    numbers: list[int]


class ListOfModelsModel(BaseModel):
    models: list[SimpleModel]


class ComplexModel(BaseModel):
    nested: NestedModel
    list_models: list[SimpleModel]
    optional: str | None
    union: str | int
    any_type: Any


class DeepNested1(BaseModel):
    value: str


class DeepNested2(BaseModel):
    nested1: DeepNested1


class DeepNested3(BaseModel):
    nested2: DeepNested2


class DeepNested4(BaseModel):
    nested3: DeepNested3


class TestBasicBreakdown:
    def test_simple_types(self):
        result = breakdown_pydantic_annotation(SimpleModel)

        assert "name" in result
        assert "age" in result
        assert "active" in result
        assert result["name"] == str
        assert result["age"] == int
        assert result["active"] == bool

    def test_list_of_basic_types(self):
        result = breakdown_pydantic_annotation(ListModel)

        assert "items" in result
        assert "numbers" in result
        assert result["items"] == [str]
        assert result["numbers"] == [int]

    def test_returns_dict(self):
        result = breakdown_pydantic_annotation(SimpleModel)

        assert isinstance(result, dict)

    def test_preserves_field_names(self):
        result = breakdown_pydantic_annotation(SimpleModel)

        expected_fields = {"name", "age", "active"}
        assert set(result.keys()) == expected_fields


class TestNestedModels:
    def test_nested_model(self):
        result = breakdown_pydantic_annotation(NestedModel)

        assert "simple" in result
        assert "description" in result
        assert isinstance(result["simple"], dict)
        assert result["description"] == str

        # Check nested model fields
        nested = result["simple"]
        assert "name" in nested
        assert "age" in nested
        assert "active" in nested

    def test_list_of_pydantic_models(self):
        result = breakdown_pydantic_annotation(ListOfModelsModel)

        assert "models" in result
        assert isinstance(result["models"], list)
        assert len(result["models"]) == 1
        assert isinstance(result["models"][0], dict)

        # Check the model structure in the list
        model_structure = result["models"][0]
        assert "name" in model_structure
        assert "age" in model_structure
        assert "active" in model_structure

    def test_recursive_nested_models(self):
        result = breakdown_pydantic_annotation(ComplexModel)

        assert "nested" in result
        assert isinstance(result["nested"], dict)

        # Check second level nesting
        nested = result["nested"]
        assert "simple" in nested
        assert isinstance(nested["simple"], dict)


class TestComplexTypes:
    def test_optional_type(self):
        result = breakdown_pydantic_annotation(ComplexModel)

        assert "optional" in result
        # Optional[str] is represented as-is in the breakdown

    def test_union_type(self):
        result = breakdown_pydantic_annotation(ComplexModel)

        assert "union" in result
        # Union types are preserved in breakdown

    def test_any_type(self):
        result = breakdown_pydantic_annotation(ComplexModel)

        assert "any_type" in result
        assert result["any_type"] == Any

    def test_list_without_type_args(self):
        class ListNoArgs(BaseModel):
            items: list

        result = breakdown_pydantic_annotation(ListNoArgs)

        assert "items" in result
        # Should handle list without args gracefully


class TestDepthControl:
    def test_max_depth_none(self):
        result = breakdown_pydantic_annotation(DeepNested4, max_depth=None)

        # Should recurse all the way down
        assert "nested3" in result
        assert isinstance(result["nested3"], dict)
        assert "nested2" in result["nested3"]

    def test_max_depth_zero(self):
        with pytest.raises(RecursionError) as exc_info:
            breakdown_pydantic_annotation(DeepNested4, max_depth=0)

        assert "Maximum recursion depth reached" in str(exc_info.value)

    def test_max_depth_one(self):
        # max_depth=1 means we can process at depth 0 only
        # When we try to recurse (depth 1), it raises
        with pytest.raises(RecursionError) as exc_info:
            breakdown_pydantic_annotation(DeepNested2, max_depth=1)

        assert "Maximum recursion depth reached" in str(exc_info.value)

    def test_max_depth_respects_current_depth(self):
        # Starting at current_depth=2 with max_depth=2 should immediately raise
        with pytest.raises(RecursionError):
            breakdown_pydantic_annotation(DeepNested4, max_depth=2, current_depth=2)

    def test_max_depth_allows_exact_depth(self):
        # With max_depth=3, should be able to recurse 3 levels
        result = breakdown_pydantic_annotation(DeepNested3, max_depth=3)

        assert "nested2" in result
        assert isinstance(result["nested2"], dict)


class TestErrorHandling:
    def test_non_pydantic_model_raises_type_error(self):
        class NotPydantic:
            name: str

        with pytest.raises(TypeError) as exc_info:
            breakdown_pydantic_annotation(NotPydantic)

        assert "Input must be a Pydantic model" in str(exc_info.value)

    def test_none_input_raises_type_error(self):
        with pytest.raises(TypeError):
            breakdown_pydantic_annotation(None)

    def test_string_input_raises_type_error(self):
        with pytest.raises(TypeError):
            breakdown_pydantic_annotation("not a model")

    def test_dict_input_raises_type_error(self):
        with pytest.raises(TypeError):
            breakdown_pydantic_annotation({"key": "value"})

    def test_instance_instead_of_class_raises_type_error(self):
        instance = SimpleModel(name="test", age=25, active=True)

        with pytest.raises(TypeError):
            breakdown_pydantic_annotation(instance)


class TestEdgeCases:
    def test_empty_model(self):
        class EmptyModel(BaseModel):
            pass

        result = breakdown_pydantic_annotation(EmptyModel)

        assert isinstance(result, dict)
        assert len(result) == 0

    def test_model_with_many_fields(self):
        class ManyFieldsModel(BaseModel):
            field1: str
            field2: int
            field3: bool
            field4: float
            field5: list[str]
            field6: int | None
            field7: Any

        result = breakdown_pydantic_annotation(ManyFieldsModel)

        assert len(result) == 7
        assert all(f"field{i}" in result for i in range(1, 8))

    def test_model_with_same_nested_model_multiple_times(self):
        class MultiReferenceModel(BaseModel):
            first: SimpleModel
            second: SimpleModel
            third: SimpleModel

        result = breakdown_pydantic_annotation(MultiReferenceModel)

        # All three should be independently broken down
        assert "first" in result
        assert "second" in result
        assert "third" in result
        assert all(isinstance(result[k], dict) for k in ["first", "second", "third"])

    def test_list_in_nested_model(self):
        class NestedWithList(BaseModel):
            items: list[str]

        class ParentModel(BaseModel):
            nested: NestedWithList

        result = breakdown_pydantic_annotation(ParentModel)

        assert "nested" in result
        assert isinstance(result["nested"], dict)
        assert "items" in result["nested"]
        assert result["nested"]["items"] == [str]

    def test_circular_reference_prevention(self):
        # max_depth is the mechanism to prevent circular reference issues
        # that max_depth is the mechanism to prevent circular reference issues

        # max_depth=5 should allow us to process DeepNested4 fully
        # DeepNested4 -> DeepNested3 -> DeepNested2 -> DeepNested1 (4 levels)
        result = breakdown_pydantic_annotation(DeepNested4, max_depth=5)

        # Should not raise, should process successfully
        assert isinstance(result, dict)
        assert "nested3" in result


class TestAnnotationPreservation:
    def test_preserves_builtin_types(self):
        class BuiltinTypes(BaseModel):
            string: str
            integer: int
            floating: float
            boolean: bool
            bytes_: bytes

        result = breakdown_pydantic_annotation(BuiltinTypes)

        assert result["string"] == str
        assert result["integer"] == int
        assert result["floating"] == float
        assert result["boolean"] == bool
        assert result["bytes_"] == bytes

    def test_preserves_generic_types(self):
        class GenericTypes(BaseModel):
            list_int: list[int]
            list_str: list[str]

        result = breakdown_pydantic_annotation(GenericTypes)

        assert result["list_int"] == [int]
        assert result["list_str"] == [str]

    def test_list_with_any_type(self):
        class ListAny(BaseModel):
            items: list[Any]

        result = breakdown_pydantic_annotation(ListAny)

        assert "items" in result
        assert result["items"] == [Any]


class TestRecursionBehavior:
    def test_current_depth_increments(self):
        # Tested indirectly through max_depth behavior
        class Level1(BaseModel):
            value: str

        class Level0(BaseModel):
            nested: Level1

        # With max_depth=1, should be able to recurse into Level1
        result = breakdown_pydantic_annotation(Level0, max_depth=2)

        assert "nested" in result
        assert isinstance(result["nested"], dict)
        assert "value" in result["nested"]

    def test_max_depth_prevents_deep_recursion(self):
        # max_depth=2 means we can process at depths 0 and 1
        # DeepNested4 has nested3 (depth 1), which has nested2 (would be depth 2)
        # So this should raise when trying to recurse into nested2
        with pytest.raises(RecursionError):
            breakdown_pydantic_annotation(DeepNested4, max_depth=2)

    def test_list_recursion_respects_depth(self):
        class DeepInList(BaseModel):
            nested: DeepNested2

        class ListOfDeep(BaseModel):
            items: list[DeepInList]

        # Should handle depth correctly when recursing into list elements
        # ListOfDeep (depth 0) -> DeepInList (depth 1) -> DeepNested2 (depth 2) -> DeepNested1 (depth 3)
        # So max_depth=4 is needed to fully process this
        result = breakdown_pydantic_annotation(ListOfDeep, max_depth=5)

        assert "items" in result
        assert isinstance(result["items"], list)
        assert len(result["items"]) == 1
        assert isinstance(result["items"][0], dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
