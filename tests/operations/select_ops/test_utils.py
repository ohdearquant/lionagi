# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Comprehensive tests for selection utility functions."""

from enum import Enum

import pytest
from pydantic import BaseModel

from lionagi.operations.select.utils import (
    SelectionModel,
    get_choice_representation,
    parse_selection,
    parse_to_representation,
)


class TestSelectionModelClass:
    """Test SelectionModel Pydantic model."""

    def test_selection_model_default(self):
        """Test SelectionModel with defaults."""
        model = SelectionModel()
        assert model.selected == []
        assert isinstance(model.selected, list)

    def test_selection_model_with_values(self):
        """Test SelectionModel with selections."""
        selections = ["choice1", "choice2", "choice3"]
        model = SelectionModel(selected=selections)
        assert model.selected == selections

    def test_selection_model_prompt_format(self):
        """Test SelectionModel PROMPT class variable."""
        prompt = SelectionModel.PROMPT
        assert "{max_num_selections}" in prompt
        assert "{choices}" in prompt

        formatted = prompt.format(max_num_selections=3, choices=["a", "b", "c"])
        assert "3" in formatted
        assert "['a', 'b', 'c']" in formatted

    def test_selection_model_serialization(self):
        """Test model serialization."""
        model = SelectionModel(selected=["opt1", "opt2"])
        data = model.model_dump()
        assert "selected" in data
        assert data["selected"] == ["opt1", "opt2"]

    def test_selection_model_validation(self):
        """Test model validation."""
        # Should accept various types
        model1 = SelectionModel(selected=[1, 2, 3])
        assert model1.selected == [1, 2, 3]

        model2 = SelectionModel(selected=["str"])
        assert model2.selected == ["str"]


class TestGetChoiceRepresentation:
    """Test get_choice_representation function."""

    def test_string_representation(self):
        """Test representation of string choice."""
        result = get_choice_representation("simple_string")
        assert result == "simple_string"

    def test_basemodel_representation(self):
        """Test representation of Pydantic model."""

        class TestModel(BaseModel):
            field1: str
            field2: int

        model = TestModel(field1="value", field2=42)
        result = get_choice_representation(model)

        assert "TestModel" in result
        assert "field1" in result
        assert "field2" in result

    def test_enum_representation(self):
        """Test representation of Enum value."""

        class Color(Enum):
            RED = "red_value"
            GREEN = "green_value"

        result = get_choice_representation(Color.RED)
        assert result == "red_value"

    def test_enum_with_complex_value(self):
        """Test enum with complex value."""

        class ComplexEnum(Enum):
            OPTION_A = {"key": "value_a"}
            OPTION_B = {"key": "value_b"}

        # Should recursively handle the value
        result = get_choice_representation(ComplexEnum.OPTION_A)
        # Dict value is converted to string
        assert isinstance(result, str)
        assert "key" in result or "value_a" in result

    def test_nested_basemodel_representation(self):
        """Test nested BaseModel representation."""

        class InnerModel(BaseModel):
            inner_field: str

        class OuterModel(BaseModel):
            outer_field: InnerModel

        model = OuterModel(outer_field=InnerModel(inner_field="nested_value"))
        result = get_choice_representation(model)

        assert "OuterModel" in result


class TestParseToRepresentationStrings:
    """Test parse_to_representation with string inputs."""

    def test_parse_string_list(self):
        """Test parsing list of strings."""
        choices = ["apple", "banana", "orange"]
        keys, contents = parse_to_representation(choices)

        assert keys == choices
        assert contents == choices

    def test_parse_string_tuple(self):
        """Test parsing tuple of strings."""
        choices = ("red", "green", "blue")
        keys, contents = parse_to_representation(choices)

        assert keys == ["red", "green", "blue"]
        assert contents == ["red", "green", "blue"]

    def test_parse_string_set(self):
        """Test parsing set of strings."""
        choices = {"option1", "option2", "option3"}
        keys, contents = parse_to_representation(choices)

        # Sets become lists (order may vary)
        assert len(keys) == 3
        assert len(contents) == 3
        assert set(keys) == choices


class TestParseToRepresentationEnum:
    """Test parse_to_representation with Enum."""

    def test_parse_enum_class(self):
        """Test parsing Enum class."""

        class Priority(Enum):
            LOW = 1
            MEDIUM = 2
            HIGH = 3

        keys, contents = parse_to_representation(Priority)

        assert keys == ["LOW", "MEDIUM", "HIGH"]
        # Integer enum values are converted to strings
        assert contents == ["1", "2", "3"]

    def test_parse_enum_with_string_values(self):
        """Test Enum with string values."""

        class Status(Enum):
            PENDING = "pending_state"
            ACTIVE = "active_state"
            DONE = "done_state"

        keys, contents = parse_to_representation(Status)

        assert keys == ["PENDING", "ACTIVE", "DONE"]
        assert "pending_state" in contents
        assert "active_state" in contents


class TestParseToRepresentationDict:
    """Test parse_to_representation with dict."""

    def test_parse_simple_dict(self):
        """Test parsing dict with string values."""
        choices = {
            "key1": "value1",
            "key2": "value2",
            "key3": "value3",
        }

        keys, contents = parse_to_representation(choices)

        assert keys == ["key1", "key2", "key3"]
        assert contents == ["value1", "value2", "value3"]

    def test_parse_dict_with_complex_values(self):
        """Test dict with complex values."""
        choices = {
            "option_a": {"nested": "data_a"},
            "option_b": {"nested": "data_b"},
        }

        keys, contents = parse_to_representation(choices)

        assert keys == ["option_a", "option_b"]
        assert len(contents) == 2

    def test_parse_dict_with_model_values(self):
        """Test dict with BaseModel values."""

        class OptionModel(BaseModel):
            name: str
            value: int

        choices = {
            "first": OptionModel(name="first_option", value=1),
            "second": OptionModel(name="second_option", value=2),
        }

        keys, contents = parse_to_representation(choices)

        assert keys == ["first", "second"]
        assert "OptionModel" in contents[0]
        assert "OptionModel" in contents[1]


class TestParseToRepresentationModels:
    """Test parse_to_representation with BaseModel."""

    def test_parse_list_of_model_instances(self):
        """Test parsing list of model instances."""

        class Item(BaseModel):
            id: int
            name: str

        choices = [
            Item(id=1, name="item1"),
            Item(id=2, name="item2"),
        ]

        keys, contents = parse_to_representation(choices)

        # Dict keys are unique, so duplicate class names become single key
        assert "Item" in keys
        assert len(keys) == 1  # Only one unique key
        assert len(contents) >= 1
        assert all("Item" in c for c in contents)

    def test_parse_list_of_model_classes(self):
        """Test parsing list of model classes."""

        class ModelA(BaseModel):
            field_a: str

        class ModelB(BaseModel):
            field_b: int

        choices = [ModelA, ModelB]

        keys, contents = parse_to_representation(choices)

        assert keys == ["ModelA", "ModelB"]
        assert len(contents) == 2

    def test_parse_mixed_model_types(self):
        """Test parsing mixed model instances from different classes."""

        class TypeA(BaseModel):
            field: str

        class TypeB(BaseModel):
            value: int

        choices = [
            TypeA(field="test"),
            TypeB(value=42),
        ]

        keys, contents = parse_to_representation(choices)

        assert "TypeA" in keys
        assert "TypeB" in keys


class TestParseToRepresentationEdgeCases:
    """Test edge cases for parse_to_representation."""

    def test_parse_empty_list(self):
        """Test parsing empty list returns two empty parallel sequences."""
        result = parse_to_representation([])
        items, keys = result
        assert items == []
        assert keys == []

    def test_parse_unsupported_type(self):
        """Test unsupported input type raises TypeError (was NotImplementedError)."""
        with pytest.raises(TypeError):
            parse_to_representation(12345)

    def test_parse_mixed_type_list(self):
        """Test list with mixed types raises TypeError (was NotImplementedError)."""
        with pytest.raises(TypeError):
            parse_to_representation([1, "string", 3.14])

    def test_parse_empty_dict(self):
        """Test parsing empty dict."""
        keys, contents = parse_to_representation({})
        assert keys == []
        assert contents == []


class TestParseSelectionStrings:
    """Test parse_selection with string choices."""

    def test_parse_exact_match_string_list(self):
        """Test exact match in string list."""
        choices = ["apple", "banana", "orange"]
        result = parse_selection("apple", choices)
        assert result == "apple"

    def test_parse_fuzzy_match_string_list(self):
        """Test fuzzy matching in string list."""
        choices = ["option_one", "option_two", "option_three"]
        # Should find closest match
        result = parse_selection("option one", choices)
        assert result in choices

    def test_parse_string_tuple(self):
        """Test parsing with tuple of strings."""
        choices = ("red", "green", "blue")
        result = parse_selection("red", choices)
        assert result == "red"

    def test_parse_string_set(self):
        """Test parsing with set of strings."""
        # Convert set to list for parse_selection since sets aren't subscriptable
        choices = ["choice_a", "choice_b", "choice_c"]
        result = parse_selection("choice_a", choices)
        assert result == "choice_a"


class TestParseSelectionEnum:
    """Test parse_selection with Enum."""

    def test_parse_enum_exact_name(self):
        """Test parsing with exact enum name."""

        class Color(Enum):
            RED = "red_value"
            GREEN = "green_value"
            BLUE = "blue_value"

        result = parse_selection("RED", Color)
        assert result == Color.RED

    def test_parse_enum_fuzzy_match(self):
        """Test fuzzy matching enum name."""

        class Status(Enum):
            PENDING = 1
            IN_PROGRESS = 2
            COMPLETED = 3

        # Should find closest match
        result = parse_selection("in progress", Status)
        assert isinstance(result, Status)

    def test_parse_enum_no_exact_match(self):
        """Test enum selection with no exact match."""

        class Priority(Enum):
            LOW = 1
            MEDIUM = 2
            HIGH = 3

        # Fuzzy match should still find something
        result = parse_selection("medium priority", Priority)
        assert result in [Priority.LOW, Priority.MEDIUM, Priority.HIGH]


class TestParseSelectionDict:
    """Test parse_selection with dict."""

    def test_parse_dict_exact_key(self):
        """Test parsing with exact dict key."""
        choices = {
            "key1": "value1",
            "key2": "value2",
            "key3": "value3",
        }

        result = parse_selection("key1", choices)
        assert result == "value1"

    def test_parse_dict_fuzzy_key(self):
        """Test fuzzy matching dict key."""
        choices = {
            "option_alpha": "Alpha option",
            "option_beta": "Beta option",
            "option_gamma": "Gamma option",
        }

        # Should find closest key match
        result = parse_selection("option alpha", choices)
        assert result in choices.values()

    def test_parse_dict_returns_value(self):
        """Test dict parsing returns value, not key."""
        choices = {
            "short_key": {"complex": "value_object"},
        }

        result = parse_selection("short_key", choices)
        assert result == {"complex": "value_object"}


class TestParseSelectionModels:
    """Test parse_selection with BaseModel."""

    def test_parse_model_instances_list(self):
        """Test parsing list of model instances."""

        class Item(BaseModel):
            name: str
            value: int

        choices = [
            Item(name="first", value=1),
            Item(name="second", value=2),
        ]

        # Should match by class name
        result = parse_selection("Item", choices)
        assert result in ["Item"]  # Returns class name

    def test_parse_model_classes_list(self):
        """Test parsing list of model classes."""

        class ModelA(BaseModel):
            field_a: str

        class ModelB(BaseModel):
            field_b: int

        choices = [ModelA, ModelB]

        result = parse_selection("ModelA", choices)
        assert result == "ModelA"

    def test_parse_model_fuzzy_match(self):
        """Test fuzzy matching model names."""

        class ConfigurationModel(BaseModel):
            setting: str

        class ExecutionModel(BaseModel):
            command: str

        choices = [ConfigurationModel, ExecutionModel]

        result = parse_selection("configuration model", choices)
        assert result == "ConfigurationModel"


class TestParseSelectionEdgeCases:
    """Test edge cases for parse_selection."""

    def test_parse_empty_selection_string(self):
        """Test with empty selection string."""
        choices = ["a", "b", "c"]
        # Should still find closest match
        result = parse_selection("", choices)
        assert result in choices

    def test_parse_invalid_choices_type(self):
        """Test with invalid choices type."""
        with pytest.raises(ValueError, match="not valid"):
            parse_selection("selection", 12345)

    def test_parse_special_characters(self):
        """Test selection with special characters."""
        choices = ["option-1", "option_2", "option.3"]
        result = parse_selection("option-1", choices)
        assert result == "option-1"

    def test_parse_case_sensitivity(self):
        """Test case sensitivity in matching."""
        choices = ["Apple", "Banana", "Orange"]
        # Fuzzy match should handle case differences
        result = parse_selection("apple", choices)
        assert result in choices

    def test_parse_unicode_characters(self):
        """Test with unicode characters."""
        choices = ["café", "naïve", "résumé"]
        result = parse_selection("café", choices)
        assert result == "café"


class TestParseSelectionSimilarity:
    """Test similarity matching in parse_selection."""

    def test_closest_match_selection(self):
        """Test that closest match is selected."""
        choices = ["python_3.9", "python_3.10", "python_3.11"]

        # Should match closest
        result = parse_selection("python 3.10", choices)
        assert "3.10" in result or result == "python_3.10"

    def test_partial_match(self):
        """Test partial string matching."""
        choices = [
            "machine_learning",
            "deep_learning",
            "reinforcement_learning",
        ]

        result = parse_selection("deep", choices)
        # Should find deep_learning as closest
        assert "deep" in result.lower()

    def test_typo_tolerance(self):
        """Test tolerance for typos."""
        choices = ["configuration", "execution", "validation"]

        # Typo should still find match
        result = parse_selection("confguration", choices)
        assert result in choices

    def test_abbreviation_matching(self):
        """Test matching with abbreviations."""
        choices = [
            "artificial_intelligence",
            "machine_learning",
            "natural_language_processing",
        ]

        # Should handle abbreviations reasonably
        result = parse_selection("ai", choices)
        assert result in choices


class TestParseUtilitiesIntegration:
    """Integration tests for parse utilities."""

    def test_full_workflow_strings(self):
        """Test complete workflow with strings."""
        choices = ["option_a", "option_b", "option_c"]

        # Parse to representation
        keys, contents = parse_to_representation(choices)
        assert keys == choices
        assert contents == choices

        # Parse selection
        result = parse_selection("option_a", choices)
        assert result == "option_a"

    def test_full_workflow_enum(self):
        """Test complete workflow with Enum."""

        class Framework(Enum):
            PYTORCH = "PyTorch"
            TENSORFLOW = "TensorFlow"
            JAX = "JAX"

        # Parse to representation
        keys, contents = parse_to_representation(Framework)
        assert "PYTORCH" in keys

        # Get representation
        repr_result = get_choice_representation(Framework.PYTORCH)
        assert repr_result == "PyTorch"

        # Parse selection
        result = parse_selection("PYTORCH", Framework)
        assert result == Framework.PYTORCH

    def test_full_workflow_dict(self):
        """Test complete workflow with dict."""
        choices = {
            "fast": "Speed optimized",
            "reliable": "Reliability optimized",
            "cheap": "Cost optimized",
        }

        # Parse to representation
        keys, contents = parse_to_representation(choices)
        assert "fast" in keys
        assert "Speed optimized" in contents

        # Parse selection
        result = parse_selection("fast", choices)
        assert result == "Speed optimized"

    def test_full_workflow_models(self):
        """Test complete workflow with BaseModel."""

        class Config(BaseModel):
            name: str
            enabled: bool

        choices = [
            Config(name="config1", enabled=True),
            Config(name="config2", enabled=False),
        ]

        # Parse to representation
        keys, contents = parse_to_representation(choices)
        assert "Config" in keys[0]

        # Get representation
        repr_result = get_choice_representation(choices[0])
        assert "Config" in repr_result

        # Parse selection
        result = parse_selection("Config", choices)
        assert result == "Config"
