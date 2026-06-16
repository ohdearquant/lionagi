"""Tests for OperableModel class."""

from typing import Any

import pytest
from pydantic import Field

from lionagi.models import FieldModel, OperableModel, SchemaModel


class TestOperableModel:
    def test_basic_model_creation(self):

        class TestModel(OperableModel):
            field1: str = "test"
            field2: int = 123

        model = TestModel()
        assert model.field1 == "test"
        assert model.field2 == 123
        assert isinstance(model.extra_fields, dict)
        assert len(model.extra_fields) == 0

    def test_extra_fields_serialization(self):

        class TestModel(OperableModel):
            base_field: str = "base"

        model = TestModel()
        model.extra_fields["extra_field"] = Field()
        object.__setattr__(model, "extra_field", "extra")

        result = model.to_dict()
        assert result["base_field"] == "base"
        assert result["extra_field"] == "extra"

    def test_nested_model_serialization(self):

        class NestedModel(SchemaModel):
            nested_field: str = "nested"

        class TestModel(OperableModel):
            base_field: str = "base"

        model = TestModel()
        nested = NestedModel()
        model.extra_fields["nested"] = Field()
        object.__setattr__(model, "nested", nested)

        result = model.to_dict()
        assert result["base_field"] == "base"
        assert isinstance(result["nested"], dict)
        assert result["nested"]["nested_field"] == "nested"

    def test_add_field_basic(self):
        model = OperableModel()
        model.extra_fields["new_field"] = Field()
        object.__setattr__(model, "new_field", "test")

        assert "new_field" in model.extra_fields
        assert model.new_field == "test"

    def test_add_field_with_annotation(self):
        model = OperableModel()
        model.add_field("int_field", value=42, annotation=int)

        assert model.extra_fields["int_field"].annotation == int
        assert model.int_field == 42

    def test_add_field_with_field_info(self):
        model = OperableModel()
        field_obj = Field(default="test", description="Test field")
        model.extra_fields["field_info_test"] = field_obj
        object.__setattr__(model, "field_info_test", "test")

        assert model.field_info_test == "test"
        assert model.extra_fields["field_info_test"].description == "Test field"

    def test_add_duplicate_field(self):
        model = OperableModel()
        model.extra_fields["test_field"] = Field()
        object.__setattr__(model, "test_field", "test")

        with pytest.raises(ValueError):
            model.add_field("test_field", value="duplicate")

    def test_update_field(self):
        model = OperableModel()
        model.extra_fields["test_field"] = Field()
        object.__setattr__(model, "test_field", "initial")

        model.update_field("test_field", value="updated")
        assert model.test_field == "updated"

    def test_update_field_attributes(self):
        model = OperableModel()
        model.extra_fields["test_field"] = Field()
        object.__setattr__(model, "test_field", "test")

        model.field_setattr("test_field", "description", "Updated description")
        assert model.extra_fields["test_field"].description == "Updated description"

    def test_field_setattr(self):
        model = OperableModel()
        model.extra_fields["test_field"] = Field()
        object.__setattr__(model, "test_field", "test")

        model.field_setattr("test_field", "description", "New description")
        assert model.extra_fields["test_field"].description == "New description"

    def test_field_getattr(self):
        model = OperableModel()
        field_info = Field(description="Test description")
        model.extra_fields["test_field"] = field_info
        object.__setattr__(model, "test_field", "test")

        assert model.field_getattr("test_field", "description") == "Test description"

        # Test with default value
        assert model.field_getattr("test_field", "nonexistent", "default") == "default"

    def test_field_hasattr(self):
        model = OperableModel()
        field_info = Field(description="Test description")
        model.extra_fields["test_field"] = field_info
        object.__setattr__(model, "test_field", "test")

        assert model.field_hasattr("test_field", "description")
        assert not model.field_hasattr("test_field", "nonexistent")

    def test_all_fields_property(self):

        class TestModel(OperableModel):
            base_field: str = "base"

        model = TestModel()
        model.extra_fields["extra_field"] = Field()
        object.__setattr__(model, "extra_field", "extra")

        all_fields = model.all_fields
        assert "base_field" in all_fields
        assert "extra_field" in all_fields
        assert "extra_fields" not in all_fields  # Should be excluded

    def test_complex_field_operations(self):
        model = OperableModel()

        # Add field with validator
        def validate_positive(value: int) -> int:
            if value <= 0:
                raise ValueError("Value must be positive")
            return value

        field_info: int = Field()
        model.extra_fields["validated_field"] = field_info
        object.__setattr__(model, "validated_field", 10)

        assert model.validated_field == 10

    def test_field_default_factory(self):
        model = OperableModel()
        field_info = Field(default_factory=list)
        model.extra_fields["list_field"] = field_info
        object.__setattr__(model, "list_field", [])

        assert isinstance(model.list_field, list)
        assert len(model.list_field) == 0

    def test_invalid_field_operations(self):
        model = OperableModel()

        # Test accessing non-existent field
        with pytest.raises(KeyError):
            model.field_getattr("nonexistent", "attr")

        # Test setting attributes on non-existent field
        with pytest.raises(KeyError):
            model.field_setattr("nonexistent", "attr", "value")

        # Test providing both default and default_factory
        with pytest.raises(ValueError):
            model.add_field("invalid_field", default="value", default_factory=list)

    def test_nested_field_updates(self):

        class NestedModel(SchemaModel):
            nested_field: str = "nested"

        model = OperableModel()
        nested = NestedModel()
        model.extra_fields["nested"] = Field()
        object.__setattr__(model, "nested", nested)

        # Update nested model
        new_nested = NestedModel(nested_field="updated")
        object.__setattr__(model, "nested", new_nested)

        assert model.nested.nested_field == "updated"
        result = model.to_dict()
        assert result["nested"]["nested_field"] == "updated"


def test_override_builtin_attribute():
    """Adding a field named __dict__ must fail (dunder attribute guard)."""
    model = OperableModel()

    # Some internal/built-in attribute name to test
    builtin_name = "__dict__"

    # Because `__dict__` is a special attribute,
    # we expect an error or unexpected behavior if we try to add it.
    with pytest.raises(AttributeError, match="Cannot directly assign to dunder fields"):
        model.add_field(builtin_name, value="should_fail")


def test_update_field_multiple_times():
    """Multiple sequential updates to the same field must all take effect."""
    model = OperableModel()

    # First addition
    model.add_field("multi_update", value=10, annotation=int)
    assert model.multi_update == 10

    # Update #1
    model.update_field("multi_update", value=20)
    assert model.multi_update == 20

    # Update #2 with different annotation
    model.update_field("multi_update", annotation=float, value=3.14)
    assert model.multi_update == 3.14
    assert model.extra_fields["multi_update"].annotation == float


def test_redefine_field_via_add_field():
    """add_field() on an existing field must raise ValueError (no duplicates)."""
    model = OperableModel()
    model.add_field("my_field", value="initial")

    with pytest.raises(ValueError, match="already exists"):
        model.add_field("my_field", value="redefined")


def test_remove_field_not_implemented():
    model = OperableModel()
    model.add_field("temp_field", value=42)
    assert model.temp_field == 42

    # There's no built-in method for removing an extra field,
    # but let's see if removing from extra_fields dict is enough.
    model.remove_field("temp_field")
    assert "temp_field" not in model.all_fields
    with pytest.raises(AttributeError):
        _ = model.temp_field  # Should no longer exist, or at least not be accessible.


def test_add_field_with_field_model():
    """FieldModel passed via field_model param wires annotation and validator."""

    def validate_positive(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Must be non-negative")
        return value

    model = OperableModel()
    field_model = FieldModel(name="score", annotation=int, validator=validate_positive)

    model.add_field("score", field_model=field_model, value=10)
    assert model.score == 10

    # Check that the validator works
    with pytest.raises(ValueError, match="non-negative"):
        model.update_field("score", value=-5)


def test_update_field_with_new_default_factory():
    """update_field can replace the default_factory of an existing field."""
    model = OperableModel()
    model.add_field("dynamic_list", value=[1, 2, 3])

    def factory_func():
        return ["a", "b", "c"]

    model.update_field("dynamic_list", default_factory=factory_func)
    assert callable(model.extra_fields["dynamic_list"].default_factory)

    # If we remove the current value to trigger a re-init
    delattr(model, "dynamic_list")
    assert model.dynamic_list == ["a", "b", "c"]


def test_update_non_existent_field_creates_new():
    """update_field on a missing field behaves like add_field."""
    model = OperableModel()

    model.update_field("newly_created", value="hello", annotation=str)
    assert model.newly_created == "hello"
    assert model.extra_fields["newly_created"].annotation == str


def test_subclass_inheritance():

    class SubOperable(OperableModel):
        def add_special_field(self, name: str, value: Any):
            # Just a convenience wrapper
            self.add_field(name, value=value)

    instance = SubOperable()
    instance.add_special_field("special", value="unique")
    assert instance.special == "unique"


def test_to_dict_with_unset_field():
    """Field added without value (stays UNDEFINED) must not appear in to_dict()."""
    model = OperableModel()
    model.add_field("unassigned_field")  # No 'value' => remains UNDEFINED

    output = model.to_dict()
    assert "unassigned_field" not in output, (
        "Field that remains UNDEFINED should not appear in to_dict() output."
    )


def test_field_getattr_looks_in_json_schema_extra():
    """field_getattr must also search json_schema_extra for stored metadata."""
    model = OperableModel()
    model.add_field("custom_meta_field", value="meta")

    # Add some arbitrary metadata
    model.field_setattr("custom_meta_field", "my_custom_meta", "cool stuff")

    # Now we retrieve it via field_getattr
    meta_value = model.field_getattr("custom_meta_field", "my_custom_meta")
    assert meta_value == "cool stuff"
