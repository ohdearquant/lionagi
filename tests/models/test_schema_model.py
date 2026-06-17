"""Tests for SchemaModel class."""

from typing import Any

import pytest
from pydantic import Field

from lionagi.models import SchemaModel
from lionagi.utils import UNDEFINED


class TestSchemaModel:
    def test_schema_model_basic(self):

        class TestSchema(SchemaModel):
            field1: str = "test"
            field2: int = 123

        model = TestSchema()
        assert model.field1 == "test"
        assert model.field2 == 123

    def test_forbid_extra_fields(self):

        class TestSchema(SchemaModel):
            field1: str

        # Should raise error when extra fields provided
        with pytest.raises(ValueError):
            TestSchema(field1="test", extra_field="extra")

        # Should work with valid fields
        model = TestSchema(field1="test")
        assert model.field1 == "test"

    def test_keys_method(self):

        class TestSchema(SchemaModel):
            field1: str = "test"
            field2: int = 123
            field3: str | None = None

        model = TestSchema()
        keys = model.keys()

        assert isinstance(keys, list)
        assert set(keys) == {"field1", "field2", "field3"}

    def test_inheritance_behavior(self):

        class TestSchema(SchemaModel):
            field1: str
            field2: str | Any = Field(default=UNDEFINED)

        model = TestSchema(field1="test")
        result = model.to_dict()

        assert "field1" in result
        assert "field2" not in result  # UNDEFINED fields should be excluded

    def test_nested_schema_models(self):

        class NestedSchema(SchemaModel):
            nested_field: str = "nested"

        class TestSchema(SchemaModel):
            field1: str = "test"
            nested: NestedSchema = Field(default_factory=NestedSchema)

        model = TestSchema()

        # Test nested model instantiation
        assert isinstance(model.nested, NestedSchema)
        assert model.nested.nested_field == "nested"

        # Test nested model in to_dict
        result = model.to_dict()
        assert isinstance(result["nested"], dict)
        assert result["nested"]["nested_field"] == "nested"

    def test_validation_behavior(self):

        class TestSchema(SchemaModel):
            age: int = Field(gt=0, lt=150)
            name: str = Field(min_length=2)

        # Valid data
        model = TestSchema(age=25, name="John")
        assert model.age == 25
        assert model.name == "John"

        # Invalid age
        with pytest.raises(ValueError):
            TestSchema(age=-1, name="John")

        # Invalid name
        with pytest.raises(ValueError):
            TestSchema(age=25, name="J")

    def test_default_validation_disabled(self):

        class TestSchema(SchemaModel):
            field1: str = "default"

        # Should not validate default values
        model = TestSchema()
        assert model.field1 == "default"

    def test_from_dict_validation(self):

        class TestSchema(SchemaModel):
            field1: str
            field2: int = Field(gt=0)

        # Valid data
        data = {"field1": "test", "field2": 10}
        model = TestSchema.from_dict(data)
        assert model.field1 == "test"
        assert model.field2 == 10

        # Invalid data
        invalid_data = {"field1": "test", "field2": -1}
        with pytest.raises(ValueError):
            TestSchema.from_dict(invalid_data)

    def test_complex_validation_rules(self):

        class TestSchema(SchemaModel):
            values: list[int] = Field(min_length=1, max_length=5)
            mapping: dict[str, str] = Field(min_length=1)

        # Valid data
        model = TestSchema(values=[1, 2, 3], mapping={"key": "value"})
        assert model.values == [1, 2, 3]
        assert model.mapping == {"key": "value"}

        # Invalid: empty list
        with pytest.raises(ValueError):
            TestSchema(values=[], mapping={"key": "value"})

        # Invalid: too many items
        with pytest.raises(ValueError):
            TestSchema(values=[1, 2, 3, 4, 5, 6], mapping={"key": "value"})

        # Invalid: empty dict
        with pytest.raises(ValueError):
            TestSchema(values=[1], mapping={})

    def test_nested_validation(self):

        class NestedSchema(SchemaModel):
            value: int = Field(gt=0)

        class TestSchema(SchemaModel):
            nested: NestedSchema

        # Valid data
        model = TestSchema(nested=NestedSchema(value=10))
        assert model.nested.value == 10

        # Invalid nested data
        with pytest.raises(ValueError):
            TestSchema(nested=NestedSchema(value=-1))

    def test_optional_fields(self):

        class TestSchema(SchemaModel):
            required: str
            optional: str | None = None

        # Test with only required field
        model = TestSchema(required="test")
        assert model.required == "test"
        assert model.optional is None

        # Test with both fields
        model = TestSchema(required="test", optional="value")
        assert model.required == "test"
        assert model.optional == "value"

    def test_field_exclusion(self):

        class TestSchema(SchemaModel):
            public: str = "public"
            private: str = Field("private", exclude=True)

        model = TestSchema()
        result = model.to_dict()

        assert "public" in result
        assert "private" not in result
