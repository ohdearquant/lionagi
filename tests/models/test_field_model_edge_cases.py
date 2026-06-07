"""Edge case tests for field_model.py."""

from __future__ import annotations

import pytest
from pydantic.fields import FieldInfo

from lionagi.models.field_model import FieldModel


def _pos_validator(v):
    return v > 0


def _raise_validator(v):
    if v < 0:
        raise ValueError("negative not allowed")
    return v


class TestFieldModelConstruction:
    def test_minimal(self):
        fm = FieldModel(base_type=str)
        assert fm.base_type is str

    def test_with_name_kwarg(self):
        fm = FieldModel(name="my_field", base_type=int)
        assert fm.name == "my_field"

    def test_default_name_is_field(self):
        fm = FieldModel(base_type=str)
        assert fm.name == "field"

    def test_annotation_alias_for_base_type(self):
        converted = FieldModel._convert_kwargs_to_params(annotation=float)
        assert converted.get("base_type") is float

    def test_with_description(self):
        fm = FieldModel(base_type=str, description="A label")
        assert fm.description == "A label"

    def test_with_default(self):
        fm = FieldModel(base_type=int, default=42)
        assert fm.default == 42

    def test_with_alias(self):
        fm = FieldModel(base_type=str, alias="alt_name")
        assert fm.alias == "alt_name"

    def test_invalid_param_not_in_metadata(self):
        converted = FieldModel._convert_kwargs_to_params(base_type=str, custom_k="v")
        meta = converted.get("metadata", ())
        assert any(m.key == "custom_k" for m in meta)

    def test_missing_attr_raises_attribute_error(self):
        fm = FieldModel(base_type=str)
        with pytest.raises(AttributeError):
            _ = fm.nonexistent_attr

    def test_cannot_set_both_default_and_default_factory(self):
        with pytest.raises(ValueError, match="both default and default_factory"):
            FieldModel(base_type=int, default=1, default_factory=list)


class TestFieldModelFluentAPI:
    def test_as_nullable_returns_new_instance(self):
        fm = FieldModel(base_type=str)
        nullable = fm.as_nullable()
        assert nullable is not fm
        assert nullable.is_nullable is True
        assert fm.is_nullable is False

    def test_as_nullable_annotation_includes_none(self):
        fm = FieldModel(base_type=int)
        nullable = fm.as_nullable()
        ann = nullable.annotation
        import types

        assert isinstance(ann, types.UnionType) or str(ann) in (
            "int | None",
            "typing.Optional[int]",
        )

    def test_as_listable_returns_new_instance(self):
        fm = FieldModel(base_type=str)
        listable = fm.as_listable()
        assert listable is not fm
        assert listable.is_listable is True
        assert fm.is_listable is False

    def test_as_listable_base_type_is_list(self):
        fm = FieldModel(base_type=str)
        listable = fm.as_listable()
        assert listable.base_type == list[str]

    def test_with_validator_stores_callable(self):
        fm = FieldModel(base_type=int)
        validated = fm.with_validator(_pos_validator)
        assert validated.has_validator() is True
        assert fm.has_validator() is False

    def test_with_description_adds_description(self):
        fm = FieldModel(base_type=str)
        described = fm.with_description("some description")
        assert described.description == "some description"

    def test_with_description_replaces_existing(self):
        fm = FieldModel(base_type=str, description="old")
        updated = fm.with_description("new")
        assert updated.description == "new"

    def test_with_alias_adds_alias(self):
        fm = FieldModel(base_type=str)
        aliased = fm.with_alias("myalias")
        assert aliased.alias == "myalias"

    def test_with_default_adds_default(self):
        fm = FieldModel(base_type=int)
        defaulted = fm.with_default(99)
        assert defaulted.default == 99

    def test_with_default_replaces_existing(self):
        fm = FieldModel(base_type=int, default=1)
        updated = fm.with_default(2)
        assert updated.default == 2

    def test_with_default_factory(self):
        fm = FieldModel(base_type=list)
        fm2 = fm.with_default(list)
        assert fm2.extract_metadata("default") is list

    def test_chaining_fluent_methods(self):
        fm = (
            FieldModel(base_type=str)
            .with_description("chained")
            .with_alias("alias_x")
            .as_nullable()
        )
        assert fm.description == "chained"
        assert fm.alias == "alias_x"
        assert fm.is_nullable is True

    def test_with_frozen(self):
        fm = FieldModel(base_type=int)
        frozen = fm.with_frozen(True)
        assert frozen.extract_metadata("frozen") is True

    def test_with_exclude(self):
        fm = FieldModel(base_type=str)
        excluded = fm.with_exclude(True)
        assert excluded.extract_metadata("exclude") is True

    def test_with_metadata_custom_key(self):
        fm = FieldModel(base_type=str)
        fm2 = fm.with_metadata("custom_key", "custom_val")
        assert fm2.extract_metadata("custom_key") == "custom_val"

    def test_with_title(self):
        fm = FieldModel(base_type=str)
        titled = fm.with_title("My Title")
        assert titled.extract_metadata("title") == "My Title"


class TestFieldModelCreateField:
    def test_returns_field_info(self):
        fm = FieldModel(base_type=str, description="desc")
        fi = fm.create_field()
        assert isinstance(fi, FieldInfo)

    def test_description_in_field_info(self):
        fm = FieldModel(base_type=str, description="my desc")
        fi = fm.create_field()
        assert fi.description == "my desc"

    def test_nullable_sets_default_none(self):
        fm = FieldModel(base_type=int).as_nullable()
        fi = fm.create_field()
        assert fi.default is None

    def test_default_value_in_field_info(self):
        fm = FieldModel(base_type=int, default=7)
        fi = fm.create_field()
        assert fi.default == 7

    def test_callable_default_becomes_factory(self):
        fm = FieldModel(base_type=list, default=list)
        fi = fm.create_field()
        assert fi.default_factory is list or fi.default is list

    def test_annotation_set_on_field_info(self):
        fm = FieldModel(base_type=str)
        fi = fm.create_field()
        assert fi.annotation is str

    def test_extra_metadata_goes_to_json_schema_extra(self):
        fm = FieldModel(base_type=str, custom_meta="value")
        fi = fm.create_field()
        assert fi.json_schema_extra is not None
        assert fi.json_schema_extra.get("custom_meta") == "value"


class TestFieldModelAnnotated:
    def test_annotated_returns_type(self):
        fm = FieldModel(base_type=str)
        result = fm.annotated()
        assert result is not None

    def test_annotated_nullable(self):
        fm = FieldModel(base_type=int).as_nullable()
        result = fm.annotated()
        import types

        assert isinstance(result, types.UnionType) or "None" in str(result)

    def test_annotated_listable(self):
        fm = FieldModel(base_type=str).as_listable()
        result = fm.annotated()
        assert result is not None

    def test_annotated_cache_same_object(self):
        fm = FieldModel(base_type=str, description="cached")
        r1 = fm.annotated()
        r2 = fm.annotated()
        assert r1 is r2


class TestFieldModelValidators:
    def test_is_valid_passes(self):
        fm = FieldModel(base_type=int).with_validator(_pos_validator)
        assert fm.is_valid(5) is True

    def test_is_valid_fails(self):
        fm = FieldModel(base_type=int).with_validator(_pos_validator)
        assert fm.is_valid(-1) is False

    def test_validator_raises_propagates(self):
        fm = FieldModel(base_type=int).with_validator(_raise_validator)
        with pytest.raises(ValueError, match="negative"):
            fm.validate(-1)

    def test_validate_passes_silently(self):
        fm = FieldModel(base_type=int).with_validator(_pos_validator)
        result = fm.validate(10)
        assert result is None

    def test_has_validator_false_without_validator(self):
        fm = FieldModel(base_type=str)
        assert fm.has_validator() is False

    def test_validate_no_validators_noop(self):
        fm = FieldModel(base_type=str)
        result = fm.validate("anything")
        assert result is None

    def test_field_validator_property_returns_dict(self):
        def my_val(v):
            return v > 0

        fm = FieldModel(name="score", base_type=int).with_validator(my_val)
        fv = fm.field_validator
        assert fv is not None

    def test_validate_pydantic_style_validator(self):
        """Pydantic-style (cls, value) validator: validation passes without raising."""
        fm = FieldModel(base_type=int).with_validator(lambda cls, v: v)
        result = fm.validate(3, field_name="score")
        assert result is None

    def test_validate_boolean_false_raises_validation_error(self):
        """Simple bool validator returning False raises ValidationError."""
        from lionagi._errors import ValidationError

        fm = FieldModel(name="score", base_type=int).with_validator(lambda v: v > 0)
        with pytest.raises(ValidationError):
            fm.validate(-1, field_name="score")


class TestFieldModelProperties:
    def test_annotation_property_str(self):
        fm = FieldModel(base_type=str)
        assert fm.annotation is str

    def test_annotation_property_listable(self):
        fm = FieldModel(base_type=int).as_listable()
        assert fm.annotation == list[int]

    def test_repr_nullable(self):
        fm = FieldModel(base_type=str).as_nullable()
        r = repr(fm)
        assert "nullable" in r

    def test_repr_listable(self):
        fm = FieldModel(base_type=str).as_listable()
        r = repr(fm)
        assert "listable" in r

    def test_repr_validated(self):
        fm = FieldModel(base_type=int).with_validator(_pos_validator)
        r = repr(fm)
        assert "validated" in r

    def test_metadata_dict(self):
        fm = FieldModel(base_type=str, description="desc", alias="al")
        d = fm.metadata_dict()
        assert d.get("description") == "desc"
        assert d.get("alias") == "al"

    def test_metadata_dict_with_exclude(self):
        fm = FieldModel(base_type=str, description="desc", alias="al")
        d = fm.metadata_dict(exclude=["alias"])
        assert "alias" not in d
        assert d.get("description") == "desc"

    def test_extract_metadata_missing_returns_none(self):
        fm = FieldModel(base_type=str)
        assert fm.extract_metadata("nonexistent") is None

    def test_to_dict_deprecated(self):
        fm = FieldModel(base_type=str, description="d")
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = fm.to_dict()
            assert any(issubclass(warning.category, DeprecationWarning) for warning in w)
        assert isinstance(result, dict)
