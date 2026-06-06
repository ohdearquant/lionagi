"""Tests for FieldModel class."""

import pytest
from pydantic.fields import FieldInfo

from lionagi.models import FieldModel


class TestFieldModel:
    """Test suite for FieldModel class."""

    def test_basic_field_creation(self):
        """Test basic field creation and attributes."""
        field = FieldModel(
            name="test_field",
            default="default_value",
            title="Test Field",
            description="A test field",
        )

        assert field.name == "test_field"
        assert field.default == "default_value"
        assert field.title == "Test Field"
        assert field.description == "A test field"

    def test_field_info_generation(self):
        """Test generation of Pydantic FieldInfo object."""
        field = FieldModel(
            name="test_field",
            default="default_value",
            title="Test Field",
            description="A test field",
        )

        field_info = field.create_field()
        assert isinstance(field_info, FieldInfo)
        assert field_info.default == "default_value"
        assert field_info.title == "Test Field"
        assert field_info.description == "A test field"

    def test_field_with_annotation(self):
        """Test field with type annotation."""
        field = FieldModel(name="test_field", annotation=int, default=42)

        field_info = field.create_field()
        assert field_info.annotation == int
        assert field_info.default == 42

    def test_field_validator_configuration(self):
        """Test field validator configuration."""

        def validate_positive(value: int) -> int:
            if value <= 0:
                raise ValueError("Value must be positive")
            return value

        field = FieldModel(name="test_field", annotation=int, validator=validate_positive)

        validator_dict = field.field_validator
        assert isinstance(validator_dict, dict)
        assert "test_field_validator" in validator_dict

    def test_field_validator_with_kwargs(self):
        """Test field validator with custom kwargs."""

        def validate_range(value: int) -> int:
            if not 0 <= value <= 100:
                raise ValueError("Value must be between 0 and 100")
            return value

        field = FieldModel(name="test_field", annotation=int, validator=validate_range)

        validator_dict = field.field_validator
        assert isinstance(validator_dict, dict)
        assert "test_field_validator" in validator_dict

    def test_complex_field_configuration(self):
        """Test complex field configuration with multiple attributes."""
        field = FieldModel(
            name="test_field",
            annotation=list[int],
            default_factory=list,
            title="Test Field",
            description="A test field",
            examples=[[1, 2, 3]],
            deprecated=False,
            exclude=False,
        )

        field_info = field.create_field()
        assert field_info.annotation == list[int]
        assert callable(field_info.default_factory)
        assert field_info.title == "Test Field"
        assert field_info.description == "A test field"
        assert field_info.examples == [[1, 2, 3]]
        assert not field_info.deprecated
        assert not field_info.exclude

    def test_field_with_alias(self):
        """Test field with alias configuration."""
        field = FieldModel(name="test_field", alias="test_alias", alias_priority=2)

        field_info = field.create_field()
        assert field_info.alias == "test_alias"
        assert field_info.alias_priority == 2

    def test_invalid_validator_configuration(self):
        """Test invalid validator configurations."""

        def invalid_validator(value: int, unknown_param: str) -> int:
            return value

        field = FieldModel(name="test_field", validator=invalid_validator)

        validator_dict = field.field_validator
        assert isinstance(validator_dict, dict)

    def test_field_inheritance(self):
        """Test field inheritance from SchemaModel."""
        field = FieldModel(name="test_field", default="test")

        # Test that extra fields are allowed
        field_with_extra = FieldModel(name="test_field", default="test", custom_attr="value")
        assert hasattr(field_with_extra, "custom_attr")

    def test_field_to_dict(self):
        """Test field serialization to dictionary."""
        field = FieldModel(
            name="test_field",
            default="default_value",
            title="Test Field",
            description="A test field",
        )
        dict_repr = field.to_dict()
        assert isinstance(dict_repr, dict)
        assert dict_repr["default"] == "default_value"
        assert dict_repr["title"] == "Test Field"
        assert dict_repr["description"] == "A test field"

    def test_field_frozen_attribute(self):
        """Test field frozen attribute."""
        field = FieldModel(name="test_field", frozen=True)

        field_info = field.create_field()
        assert field_info.frozen

        field = FieldModel(name="test_field", frozen=False)

        field_info = field.create_field()
        assert not field_info.frozen

    def test_field_default_factory(self):
        """Test field with default_factory."""

        def create_list():
            return [1, 2, 3]

        field = FieldModel(name="test_field", default_factory=create_list)

        field_info = field.create_field()
        assert callable(field_info.default_factory)
        assert field_info.default_factory() == [1, 2, 3]

    def test_field_with_examples(self):
        """Test field with examples."""
        field = FieldModel(name="test_field", examples=["example1", "example2"])

        field_info = field.create_field()
        assert field_info.examples == ["example1", "example2"]

    def test_field_with_description(self):
        """Test field with description."""
        field = FieldModel(name="test_field", description="Test description")

        field_info = field.create_field()
        assert field_info.description == "Test description"


def test_both_default_and_default_factory():
    """
    Test that defining both `default` and `default_factory` at the same time
    raises an error or is otherwise disallowed.
    """

    def factory_func():
        return "factory_value"

    with pytest.raises(ValueError, match="Cannot have both default and default_factory"):
        FieldModel(
            name="conflicting_field",
            default="some_value",
            default_factory=factory_func,
        )


@pytest.mark.parametrize("invalid_value", [123, [lambda x: x, 123], object()])
def test_invalid_validators_argument(invalid_value):
    """
    Test passing an invalid type or structure to `validators`.
    Must raise ValueError stating "Validators must be a list of functions or a function".
    """
    with pytest.raises(ValueError):
        FieldModel(name="bad_validators", validator=invalid_value)


def test_exclude_field_behavior():
    """
    Test that if `exclude=True` is set, the FieldInfo reflects that field should be excluded
    from serialization.
    """
    field = FieldModel(name="excluded_field", exclude=True)
    info = field.create_field()
    assert info.exclude is True, "Expected the field's FieldInfo to have exclude=True"


@pytest.mark.parametrize(
    "annotation, default_value",
    [
        (int, "not_an_int"),
        (str, 123),
        (
            list[int],
            ["1", 2, 3],
        ),  # The first element is a string instead of int
    ],
)
def test_type_mismatch_between_annotation_and_default(annotation, default_value):
    """
    Test providing a default value that doesn't match the annotation type
    (e.g., str default for an int annotation).
    Depending on usage, you might want to allow or disallow this scenario.
    For demonstration, we expect Pydantic won't error at FieldModel creation time,
    but it may cause an error once integrated in an actual Pydantic model.
    We'll simply confirm the mismatch is stored or accepted at this step.
    """
    field = FieldModel(name="mismatch_field", annotation=annotation, default=default_value)
    info = field.create_field()
    # The FieldInfo has the mismatch, but Pydantic doesn't strictly validate here in FieldModel alone.
    # You might consider implementing your own check if you want to raise an error now.
    assert info.annotation == annotation
    assert info.default == default_value

    # If you want to raise an error now, you'd do so in a custom validator here,
    # e.g., if "not isinstance(default_value, annotation)" then error.
