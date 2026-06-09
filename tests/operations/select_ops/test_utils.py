# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Comprehensive tests for selection utility functions."""

from enum import Enum

import pytest
from pydantic import BaseModel

from lionagi.operations.select.utils import (
    get_choice_representation,
    parse_selection,
    parse_to_representation,
)


class TestGetChoiceRepresentation:
    def test_string_representation(self):
        result = get_choice_representation("simple_string")
        assert result == "simple_string"

    def test_basemodel_representation(self):

        class TestModel(BaseModel):
            field1: str
            field2: int

        model = TestModel(field1="value", field2=42)
        result = get_choice_representation(model)

        assert "TestModel" in result
        assert "field1" in result
        assert "field2" in result

    def test_enum_representation(self):

        class Color(Enum):
            RED = "red_value"
            GREEN = "green_value"

        result = get_choice_representation(Color.RED)
        assert result == "red_value"

    def test_enum_with_complex_value(self):

        class ComplexEnum(Enum):
            OPTION_A = {"key": "value_a"}
            OPTION_B = {"key": "value_b"}

        # Should recursively handle the value
        result = get_choice_representation(ComplexEnum.OPTION_A)
        # Dict value is converted to string
        assert isinstance(result, str)
        assert "key" in result or "value_a" in result

    def test_nested_basemodel_representation(self):

        class InnerModel(BaseModel):
            inner_field: str

        class OuterModel(BaseModel):
            outer_field: InnerModel

        model = OuterModel(outer_field=InnerModel(inner_field="nested_value"))
        result = get_choice_representation(model)

        assert "OuterModel" in result


class TestParseToRepresentationStrings:
    def test_parse_string_list(self):
        choices = ["apple", "banana", "orange"]
        keys, contents = parse_to_representation(choices)

        assert keys == choices
        assert contents == choices

    def test_parse_string_tuple(self):
        choices = ("red", "green", "blue")
        keys, contents = parse_to_representation(choices)

        assert keys == ["red", "green", "blue"]
        assert contents == ["red", "green", "blue"]

    def test_parse_string_set(self):
        choices = {"option1", "option2", "option3"}
        keys, contents = parse_to_representation(choices)

        # Sets become lists (order may vary)
        assert len(keys) == 3
        assert len(contents) == 3
        assert set(keys) == choices


class TestParseToRepresentationEnum:
    def test_parse_enum_class(self):

        class Priority(Enum):
            LOW = 1
            MEDIUM = 2
            HIGH = 3

        keys, contents = parse_to_representation(Priority)

        assert keys == ["LOW", "MEDIUM", "HIGH"]
        # Integer enum values are converted to strings
        assert contents == ["1", "2", "3"]

    def test_parse_enum_with_string_values(self):

        class Status(Enum):
            PENDING = "pending_state"
            ACTIVE = "active_state"
            DONE = "done_state"

        keys, contents = parse_to_representation(Status)

        assert keys == ["PENDING", "ACTIVE", "DONE"]
        assert "pending_state" in contents
        assert "active_state" in contents


class TestParseToRepresentationDict:
    def test_parse_simple_dict(self):
        choices = {
            "key1": "value1",
            "key2": "value2",
            "key3": "value3",
        }

        keys, contents = parse_to_representation(choices)

        assert keys == ["key1", "key2", "key3"]
        assert contents == ["value1", "value2", "value3"]

    def test_parse_dict_with_complex_values(self):
        choices = {
            "option_a": {"nested": "data_a"},
            "option_b": {"nested": "data_b"},
        }

        keys, contents = parse_to_representation(choices)

        assert keys == ["option_a", "option_b"]
        assert len(contents) == 2

    def test_parse_dict_with_model_values(self):

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
    def test_parse_list_of_model_instances(self):

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

        class ModelA(BaseModel):
            field_a: str

        class ModelB(BaseModel):
            field_b: int

        choices = [ModelA, ModelB]

        keys, contents = parse_to_representation(choices)

        assert keys == ["ModelA", "ModelB"]
        assert len(contents) == 2

    def test_parse_mixed_model_types(self):

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
    def test_parse_empty_list(self):
        result = parse_to_representation([])
        items, keys = result
        assert items == []
        assert keys == []

    def test_parse_unsupported_type(self):
        with pytest.raises(TypeError):
            parse_to_representation(12345)

    def test_parse_mixed_type_list(self):
        with pytest.raises(TypeError):
            parse_to_representation([1, "string", 3.14])

    def test_parse_empty_dict(self):
        keys, contents = parse_to_representation({})
        assert keys == []
        assert contents == []


class TestParseSelectionStrings:
    def test_parse_exact_match_string_list(self):
        choices = ["apple", "banana", "orange"]
        result = parse_selection("apple", choices)
        assert result == "apple"

    def test_parse_fuzzy_match_string_list(self):
        choices = ["option_one", "option_two", "option_three"]
        # Should find closest match
        result = parse_selection("option one", choices)
        assert result in choices

    def test_parse_string_tuple(self):
        choices = ("red", "green", "blue")
        result = parse_selection("red", choices)
        assert result == "red"

    def test_parse_string_set(self):
        # Convert set to list for parse_selection since sets aren't subscriptable
        choices = ["choice_a", "choice_b", "choice_c"]
        result = parse_selection("choice_a", choices)
        assert result == "choice_a"


class TestParseSelectionEnum:
    def test_parse_enum_exact_name(self):

        class Color(Enum):
            RED = "red_value"
            GREEN = "green_value"
            BLUE = "blue_value"

        result = parse_selection("RED", Color)
        assert result == Color.RED

    def test_parse_enum_fuzzy_match(self):

        class Status(Enum):
            PENDING = 1
            IN_PROGRESS = 2
            COMPLETED = 3

        # Should find closest match
        result = parse_selection("in progress", Status)
        assert isinstance(result, Status)

    def test_parse_enum_no_exact_match(self):

        class Priority(Enum):
            LOW = 1
            MEDIUM = 2
            HIGH = 3

        # Fuzzy match should still find something
        result = parse_selection("medium priority", Priority)
        assert result in [Priority.LOW, Priority.MEDIUM, Priority.HIGH]


class TestParseSelectionDict:
    def test_parse_dict_exact_key(self):
        choices = {
            "key1": "value1",
            "key2": "value2",
            "key3": "value3",
        }

        result = parse_selection("key1", choices)
        assert result == "value1"

    def test_parse_dict_fuzzy_key(self):
        choices = {
            "option_alpha": "Alpha option",
            "option_beta": "Beta option",
            "option_gamma": "Gamma option",
        }

        # Should find closest key match
        result = parse_selection("option alpha", choices)
        assert result in choices.values()

    def test_parse_dict_returns_value(self):
        choices = {
            "short_key": {"complex": "value_object"},
        }

        result = parse_selection("short_key", choices)
        assert result == {"complex": "value_object"}


class TestParseSelectionModels:
    def test_parse_model_instances_list(self):

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

        class ModelA(BaseModel):
            field_a: str

        class ModelB(BaseModel):
            field_b: int

        choices = [ModelA, ModelB]

        result = parse_selection("ModelA", choices)
        assert result == "ModelA"

    def test_parse_model_fuzzy_match(self):

        class ConfigurationModel(BaseModel):
            setting: str

        class ExecutionModel(BaseModel):
            command: str

        choices = [ConfigurationModel, ExecutionModel]

        result = parse_selection("configuration model", choices)
        assert result == "ConfigurationModel"


class TestParseSelectionEdgeCases:
    def test_parse_empty_selection_string(self):
        choices = ["a", "b", "c"]
        # Should still find closest match
        result = parse_selection("", choices)
        assert result in choices

    def test_parse_invalid_choices_type(self):
        with pytest.raises(ValueError, match="not valid"):
            parse_selection("selection", 12345)

    def test_parse_special_characters(self):
        choices = ["option-1", "option_2", "option.3"]
        result = parse_selection("option-1", choices)
        assert result == "option-1"

    def test_parse_case_sensitivity(self):
        choices = ["Apple", "Banana", "Orange"]
        # Fuzzy match should handle case differences
        result = parse_selection("apple", choices)
        assert result in choices

    def test_parse_unicode_characters(self):
        choices = ["café", "naïve", "résumé"]
        result = parse_selection("café", choices)
        assert result == "café"


class TestParseSelectionSimilarity:
    def test_closest_match_selection(self):
        choices = ["python_3.9", "python_3.10", "python_3.11"]

        # Should match closest
        result = parse_selection("python 3.10", choices)
        assert "3.10" in result or result == "python_3.10"

    def test_partial_match(self):
        choices = [
            "machine_learning",
            "deep_learning",
            "reinforcement_learning",
        ]

        result = parse_selection("deep", choices)
        # Should find deep_learning as closest
        assert "deep" in result.lower()

    def test_typo_tolerance(self):
        choices = ["configuration", "execution", "validation"]

        # Typo should still find match
        result = parse_selection("confguration", choices)
        assert result in choices

    def test_abbreviation_matching(self):
        choices = [
            "artificial_intelligence",
            "machine_learning",
            "natural_language_processing",
        ]

        # Should handle abbreviations reasonably
        result = parse_selection("ai", choices)
        assert result in choices


class TestParseUtilitiesIntegration:
    def test_full_workflow_strings(self):
        choices = ["option_a", "option_b", "option_c"]

        # Parse to representation
        keys, contents = parse_to_representation(choices)
        assert keys == choices
        assert contents == choices

        # Parse selection
        result = parse_selection("option_a", choices)
        assert result == "option_a"

    def test_full_workflow_enum(self):

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
