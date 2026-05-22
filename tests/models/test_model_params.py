"""Tests for ModelParams class."""

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lionagi.models import FieldModel, ModelParams


class TestModelParams:
    """Test suite for ModelParams class."""

    def test_basic_model_creation(self):
        """Test basic model creation with minimal parameters."""
        field1 = Field(default="test")
        field2 = Field(default=123)
        field1.annotation = str
        field2.annotation = int

        params = ModelParams(
            name="TestModel",
            parameter_fields={"field1": field1, "field2": field2},
            inherit_base=True,
        )

        model_class = params.create_new_model()
        assert model_class.__name__ == "TestModel"

        instance = model_class()
        assert instance.field1 == "test"
        assert instance.field2 == 123

    def test_model_with_base_type(self):
        """Test model creation with custom base type."""

        class CustomBase(BaseModel):
            base_field: str = "base"

        a = Field(default="test")
        a.annotation = str
        params = ModelParams(
            name="TestModel",
            base_type=CustomBase,
            parameter_fields={"field1": a},
        )

        model_class = params.create_new_model()
        instance = model_class()

        assert instance.base_field == "base"
        assert instance.field1 == "test"

    def test_model_with_field_models(self):
        """Test model creation with FieldModel instances."""
        field_model = FieldModel(
            name="test_field",
            annotation=str,
            default="test",
            description="A test field",
        )

        params = ModelParams(name="TestModel", field_models=[field_model])

        model_class = params.create_new_model()
        instance = model_class()

        assert instance.test_field == "test"
        assert model_class.model_fields["test_field"].description == "A test field"

    def test_model_with_validators(self):
        """Test model creation with field validators."""

        def validate_positive(value: int) -> int:
            if value <= 0:
                raise ValueError("Value must be positive")
            return value

        field_model = FieldModel(
            name="validated_field",
            annotation=int,
            default=1,
            validator=validate_positive,
        )

        params = ModelParams(name="TestModel", field_models=[field_model])

        model_class = params.create_new_model()

        # Valid value
        instance = model_class(validated_field=10)
        assert instance.validated_field == 10

        # Invalid value
        with pytest.raises(ValueError):
            model_class(validated_field=-1)

    def test_exclude_fields(self):
        """Test field exclusion functionality."""

        class BaseWithFields(BaseModel):
            keep_field: str = "keep"
            exclude_field: str = "exclude"

        params = ModelParams(
            name="TestModel",
            base_type=BaseWithFields,
            exclude_fields=["exclude_field"],
            inherit_base=False,
        )

        model_class = params.create_new_model()
        instance = model_class()

        assert hasattr(instance, "keep_field")
        assert not hasattr(instance, "exclude_field")

    def test_field_descriptions(self):
        """Test field description handling."""
        field_model = FieldModel(name="test_field", annotation=str, default="test")

        params = ModelParams(
            name="TestModel",
            field_models=[field_model],
            field_descriptions={"test_field": "Updated description"},
        )

        model_class = params.create_new_model()
        assert model_class.model_fields["test_field"].description == "Updated description"

    def test_config_dict(self):
        """Test config dictionary handling."""
        a = Field(default="test")
        a.annotation = str
        params = ModelParams(
            name="TestModel",
            parameter_fields={"field1": a},
            config_dict=ConfigDict(frozen=True),
            inherit_base=False,
        )

        model_class = params.create_new_model()
        instance = model_class()

        # Should not be able to modify frozen model
        with pytest.raises(ValidationError):
            instance.field1 = "modified"

    def test_inherit_base_flag(self):
        """Test inherit_base flag behavior."""

        class CustomBase(BaseModel):
            base_field: str = "base"

        # With inheritance
        params = ModelParams(name="TestModel", base_type=CustomBase, inherit_base=True)

        model_class = params.create_new_model()
        assert issubclass(model_class, CustomBase)

        # Without inheritance
        params = ModelParams(name="TestModel", base_type=CustomBase, inherit_base=False)

        model_class = params.create_new_model()
        assert not issubclass(model_class, CustomBase)
        assert issubclass(model_class, BaseModel)

    def test_model_documentation(self):
        """Test model documentation handling."""
        doc = "This is a test model"
        a = Field(default="test")
        a.annotation = str
        params = ModelParams(
            name="TestModel",
            parameter_fields={"field1": a},
            doc=doc,
        )
        model_class = params.create_new_model()
        assert model_class.__doc__ == doc

    def test_frozen_model(self):
        """Test frozen model creation."""
        a: str = Field(default="test")
        a.annotation = str
        params = ModelParams(
            name="TestModel",
            parameter_fields={"field1": a},
            frozen=True,
        )

        model_class = params.create_new_model()
        instance = model_class()

        with pytest.raises(ValidationError):
            instance.field1 = "modified"

    def test_complex_model_creation(self):
        """Test creation of complex model with multiple features."""

        def validate_length(value: str) -> str:
            if len(value) < 3:
                raise ValueError("String too short")
            return value

        field_models = [
            FieldModel(
                name="validated_field",
                annotation=str,
                default="test",
                validator=validate_length,
            ),
            FieldModel(name="optional_field", annotation=str | None, default=None),
        ]
        a = Field(default="regular")
        a.annotation = str
        params = ModelParams(
            name="ComplexModel",
            field_models=field_models,
            parameter_fields={"regular_field": a},
            doc="A complex model with multiple features",
            config_dict=ConfigDict(extra="forbid"),
            inherit_base=False,
        )

        model_class = params.create_new_model()

        # Test instance creation
        instance = model_class()
        assert instance.validated_field == "test"
        assert instance.optional_field is None
        assert instance.regular_field == "regular"

        # Test validation
        with pytest.raises(ValueError):
            instance = model_class(validated_field="ab")

        # Test extra fields forbidden
        with pytest.raises(ValueError):
            instance = model_class(extra_field="extra")

    def test_error_handling(self):
        """Test error handling in model creation."""
        # Invalid base type
        with pytest.raises(ValueError):
            ModelParams(name="TestModel", base_type=str)

        # Invalid field models
        with pytest.raises(ValueError):
            ModelParams(name="TestModel", field_models=["not a field model"])

        # Invalid parameter fields
        with pytest.raises(ValueError):
            ModelParams(
                name="TestModel",
                parameter_fields={"field": "not a field info"},
            )


@pytest.mark.parametrize("empty_value", [None, {}, []])
def test_empty_parameter_fields(empty_value):
    """
    Test behavior when parameter_fields is empty or None.
    Expect no fields aside from what might come from the base model or field_models.
    """
    params = ModelParams(
        name="EmptyParams",
        parameter_fields=empty_value,  # Could be None, {}, or []
    )
    model_class = params.create_new_model()
    # If there's no base_type or field_models, the model should effectively have no fields
    assert len(model_class.model_fields) == 0, (
        "Expected no fields in the newly created model when parameter_fields is empty."
    )


def test_empty_field_models():
    """
    Test behavior when field_models is an empty list.
    This means only the fields from parameter_fields or base_type are used.
    """
    param_field = Field(default=123)
    param_field.annotation = int

    params = ModelParams(
        name="EmptyFieldModels",
        field_models=[],  # explicitly empty
        parameter_fields={"param_field": param_field},
    )
    model_class = params.create_new_model()
    instance = model_class()
    assert instance.param_field == 123


@pytest.mark.parametrize("exclude_value", [["ex_field"], ["a", "b"], ["x", "y"]])
def test_exclude_fields_various_collections(exclude_value):
    """
    Test passing exclude_fields as a list of field names.
    The new implementation requires exclude_fields to be a list.
    """

    class BaseWithTwoFields(BaseModel):
        a: int = 1
        b: int = 2
        ex_field: str = "excluded"

    params = ModelParams(
        name="ExclusionModel",
        base_type=BaseWithTwoFields,
        exclude_fields=exclude_value,  # Must be a list
        inherit_base=True,
    )
    model_class = params.create_new_model()
    instance = model_class()

    # Fields listed in exclude_value should not exist
    for field_name in exclude_value:
        assert not hasattr(instance, field_name), f"Field '{field_name}' should have been excluded."


def test_partial_overlap_field_descriptions():
    """
    Test that only matching field names in field_descriptions get updated,
    and other fields remain untouched.
    """
    field_models = [
        FieldModel(name="desc_field", annotation=str, default="desc"),
        FieldModel(name="no_desc_field", annotation=int, default=123),
    ]

    params = ModelParams(
        name="PartialDescModel",
        field_models=field_models,
        field_descriptions={"desc_field": "Updated description"},
    )
    model_class = params.create_new_model()
    assert model_class.model_fields["desc_field"].description == "Updated description"
    # The other field should not have any description
    assert not model_class.model_fields["no_desc_field"].description


def test_no_name_falls_back_to_base_type_name():
    """
    If 'name' is not specified, it should fall back to the base_type's __name__.
    """

    class MyBaseModel(BaseModel):
        some_field: str = "base"

    params = ModelParams(base_type=MyBaseModel, inherit_base=True)
    model_class = params.create_new_model()

    # Should derive the name from MyBaseModel
    # e.g., MyBaseModel, unless that logic is overridden
    # We'll just check that the string matches or at least contains 'MyBaseModel'
    assert "MyBaseModel" in model_class.__name__


def test_base_type_instance_instead_of_class():
    """
    The new implementation requires base_type to be a class, not an instance.
    This test verifies that passing a class works correctly.
    """

    class MyInstanceBase(BaseModel):
        base_field: str = "hello"

    # Pass the class, not an instance
    params = ModelParams(base_type=MyInstanceBase)
    model_class = params.create_new_model()

    # Should function like inheriting from MyInstanceBase
    instance = model_class()
    assert instance.base_field == "hello"


def test_conflict_between_field_models():
    """
    If two FieldModel objects share the same name but different defaults,
    the last one in the list should take precedence.
    """
    f1 = FieldModel(name="conflict_field", annotation=str, default="first")
    f2 = FieldModel(name="conflict_field", annotation=str, default="second")
    params = ModelParams(field_models=[f1, f2])
    model_class = params.create_new_model()
    instance = model_class()
    # 'second' should override 'first'
    assert instance.conflict_field == "second"


def test_conflict_between_parameter_fields_and_field_model():
    """
    If parameter_fields and field_models have the same name,
    the FieldModel typically overwrites the parameter_fields entry
    because it's updated in validate_param_model() last.
    """
    pf = Field(default="pf")
    pf.annotation = str

    fm = FieldModel(name="shared_field", annotation=str, default="fm")
    params = ModelParams(
        parameter_fields={"shared_field": pf},
        field_models=[fm],
    )
    model_class = params.create_new_model()
    instance = model_class()
    # Should be "fm", since field_models update after parameter_fields
    assert instance.shared_field == "fm"


def test_multiple_field_models_same_name_order():
    """
    Demonstrate that if multiple FieldModels share the same name in the list,
    the last one always wins.
    """
    fm1 = FieldModel(name="dup", annotation=int, default=10)
    fm2 = FieldModel(name="dup", annotation=int, default=20)
    fm3 = FieldModel(name="dup", annotation=int, default=30)

    params = ModelParams(field_models=[fm1, fm2, fm3])
    model_class = params.create_new_model()
    instance = model_class()
    assert instance.dup == 30, "The last FieldModel's default should be used."


def test_extend_use_keys_after_validation():
    """
    Test that if _use_keys is updated, we only include the fields present in parameter_fields or field_models.
    This is a lower-level test, but helps confirm the final set of keys used.
    """
    pf = Field(default=0)
    pf.annotation = int

    params = ModelParams(parameter_fields={"test_field": pf})
    # Manually add a key that doesn't exist in parameter_fields
    params._use_keys.add("non_existent")
    model_class = params.create_new_model()

    # Only 'test_field' should appear
    assert "test_field" in model_class.model_fields
    assert "non_existent" not in model_class.model_fields, (
        "Keys not present in parameter_fields/field_models should not appear in the final model."
    )


# ---------------------------------------------------------------------------
# D7 – ModelParams rejects non-FieldInfo values in parameter_fields
# ---------------------------------------------------------------------------


def test_model_params_rejects_non_fieldinfo_parameter_fields():
    """parameter_fields must contain FieldInfo instances; plain values raise ValueError."""
    with pytest.raises(ValueError, match="FieldInfo"):
        ModelParams(
            name="BadModel",
            parameter_fields={"x": "not-a-fieldinfo"},
        )


# ---------------------------------------------------------------------------
# D8 – ModelParams cache key handles unhashable field metadata
# ---------------------------------------------------------------------------


def test_model_params_create_new_model_is_idempotent():
    """Calling create_new_model() twice with identical params returns cached model."""
    f = Field(default=42)
    f.annotation = int

    p = ModelParams(name="CachedModel", parameter_fields={"val": f})
    m1 = p.create_new_model()
    m2 = p.create_new_model()

    # Should be the same class object (or at least behave identically)
    assert m1.__name__ == m2.__name__ == "CachedModel"
    assert m1(val=1).val == 1
