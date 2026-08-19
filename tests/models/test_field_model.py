"""Tests for FieldModel class."""

import math
from typing import Any

import pytest
from pydantic.fields import FieldInfo

from lionagi.ln.types import Meta, Undefined, Unset
from lionagi.models import FieldModel


class TestFieldModel:
    def test_construction_warns_with_neutral_replacement(self):
        with pytest.warns(DeprecationWarning, match="use Spec"):
            FieldModel(int, name="value")

    def test_basic_field_creation(self):
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
        field = FieldModel(name="test_field", annotation=int, default=42)

        field_info = field.create_field()
        assert field_info.annotation == int
        assert field_info.default == 42

    def test_field_validator_configuration(self):

        def validate_positive(value: int) -> int:
            if value <= 0:
                raise ValueError("Value must be positive")
            return value

        field = FieldModel(name="test_field", annotation=int, validator=validate_positive)

        validator_dict = field.field_validator
        assert isinstance(validator_dict, dict)
        assert "test_field_validator" in validator_dict

    def test_field_validator_with_kwargs(self):

        def validate_range(value: int) -> int:
            if not 0 <= value <= 100:
                raise ValueError("Value must be between 0 and 100")
            return value

        field = FieldModel(name="test_field", annotation=int, validator=validate_range)

        validator_dict = field.field_validator
        assert isinstance(validator_dict, dict)
        assert "test_field_validator" in validator_dict

    def test_compatibility_projection_preserves_last_validator_wins(self):
        from lionagi.models._build_model import build_model_type

        def add_one(value: int) -> int:
            return value + 1

        def double(value: int) -> int:
            return value * 2

        field = FieldModel(int, name="value", validator=add_one).with_validator(double)

        model_type = build_model_type(name="LegacyValidatorModel", field_models=[field])

        assert model_type(value=3).value == 6
        assert field.to_spec().get("validator") is double

    def test_compatibility_projection_preserves_last_ordinary_metadata_value(self):
        field = FieldModel(
            int,
            metadata=(
                Meta("name", "value"),
                Meta("description", "first"),
                Meta("description", "last"),
            ),
        )

        assert field.create_field().description == "last"
        assert field.to_spec().get("description") == "last"

    def test_complex_field_configuration(self):
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
        field = FieldModel(name="test_field", alias="test_alias", alias_priority=2)

        field_info = field.create_field()
        assert field_info.alias == "test_alias"
        assert field_info.alias_priority == 2

    def test_invalid_validator_configuration(self):

        def invalid_validator(value: int, unknown_param: str) -> int:
            return value

        field = FieldModel(name="test_field", validator=invalid_validator)

        validator_dict = field.field_validator
        assert isinstance(validator_dict, dict)

    def test_field_inheritance(self):
        field = FieldModel(name="test_field", default="test")

        # Test that extra fields are allowed
        field_with_extra = FieldModel(name="test_field", default="test", custom_attr="value")
        assert hasattr(field_with_extra, "custom_attr")

    def test_field_metadata_dict(self):
        field = FieldModel(
            name="test_field",
            default="default_value",
            title="Test Field",
            description="A test field",
        )
        dict_repr = field.metadata_dict()
        assert isinstance(dict_repr, dict)
        assert dict_repr["default"] == "default_value"
        assert dict_repr["title"] == "Test Field"
        assert dict_repr["description"] == "A test field"

    def test_field_frozen_attribute(self):
        field = FieldModel(name="test_field", frozen=True)

        field_info = field.create_field()
        assert field_info.frozen

        field = FieldModel(name="test_field", frozen=False)

        field_info = field.create_field()
        assert not field_info.frozen

    def test_field_default_factory(self):

        def create_list():
            return [1, 2, 3]

        field = FieldModel(name="test_field", default_factory=create_list)

        field_info = field.create_field()
        assert callable(field_info.default_factory)
        assert field_info.default_factory() == [1, 2, 3]

    def test_field_with_examples(self):
        field = FieldModel(name="test_field", examples=["example1", "example2"])

        field_info = field.create_field()
        assert field_info.examples == ["example1", "example2"]

    def test_field_with_description(self):
        field = FieldModel(name="test_field", description="Test description")

        field_info = field.create_field()
        assert field_info.description == "Test description"

    def test_with_updates_preserves_unset_metadata(self):
        field = FieldModel(base_type=int)

        updated = field.with_updates(base_type=str)

        assert updated.base_type is str
        assert updated._is_sentinel(updated.metadata)

    def test_init_delegation_validates_exactly_once(self):
        class CountingFieldModel(FieldModel):
            validation_calls = 0

            def _validate(self):
                type(self).validation_calls += 1
                super()._validate()

        CountingFieldModel(base_type=int)

        assert CountingFieldModel.validation_calls == 1

    def test_annotated_cache_distinguishes_bool_and_int_metadata(self):
        boolean = FieldModel(int, cache_probe=True).annotated()
        integer = FieldModel(int, cache_probe=1).annotated()

        assert boolean is not integer
        assert boolean.__metadata__[0].value is True
        assert integer.__metadata__[0].value == 1
        assert type(integer.__metadata__[0].value) is int

    def test_annotated_cache_distinguishes_signed_zero_metadata(self):
        positive = FieldModel(int, cache_probe=0.0).annotated()
        negative = FieldModel(int, cache_probe=-0.0).annotated()

        assert positive is not negative
        assert math.copysign(1.0, positive.__metadata__[0].value) == 1.0
        assert math.copysign(1.0, negative.__metadata__[0].value) == -1.0

    def test_mutable_metadata_opts_out_of_annotated_cache(self):
        field = FieldModel(int, payload={"value": 1})

        assert field.annotated() is not field.annotated()

    def test_annotated_cache_keys_base_types_by_identity(self):
        class EqualType(type):
            def __eq__(cls, other):
                return isinstance(other, EqualType)

            def __hash__(cls):
                return 1

        class Alpha(metaclass=EqualType):
            pass

        class Beta(metaclass=EqualType):
            pass

        alpha = FieldModel(Alpha, marker="same").annotated()
        beta = FieldModel(Beta, marker="same").annotated()

        assert alpha is not beta
        assert alpha.__origin__ is Alpha
        assert beta.__origin__ is Beta

    def test_annotated_cache_uses_effective_subclass_sentinel_policy(self):
        class UnresolvedField(FieldModel):
            @classmethod
            def _is_sentinel(cls, value):
                return value is int or super()._is_sentinel(value)

        class ConcreteField(FieldModel):
            pass

        unresolved = UnresolvedField(int, marker="same").annotated()
        concrete = ConcreteField(int, marker="same").annotated()

        assert unresolved is not concrete
        assert unresolved.__origin__ is Any
        assert concrete.__origin__ is int


def test_both_default_and_default_factory():
    """Both default and default_factory at the same time must raise ValueError."""

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
    """Invalid validator type must raise ValueError."""
    with pytest.raises(ValueError):
        FieldModel(name="bad_validators", validator=invalid_value)


def test_exclude_field_behavior():
    """exclude=True must be reflected in the created FieldInfo."""
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
    """Mismatched annotation/default is accepted at FieldModel creation; Pydantic validates later."""
    field = FieldModel(name="mismatch_field", annotation=annotation, default=default_value)
    info = field.create_field()
    assert info.annotation == annotation
    assert info.default == default_value


class _SpoofUnionMeta(type):
    """A metaclass whose str() mimics types.UnionType without being one."""

    def __repr__(cls):
        return "<class 'types.UnionType'>"

    __str__ = __repr__


class _SpoofUnion(metaclass=_SpoofUnionMeta):
    pass


def test_base_type_rejects_spoofed_union():
    """A non-type object whose str(type(...)) mimics types.UnionType is rejected."""
    spoof = _SpoofUnion()  # str(type(spoof)) == "<class 'types.UnionType'>"
    assert str(type(spoof)) == "<class 'types.UnionType'>"
    with pytest.raises(ValueError, match="base_type must be"):
        FieldModel(base_type=spoof)


def test_base_type_accepts_real_unions():
    """Genuine PEP 604 and Optional-style unions remain valid base_types."""
    from typing import Optional, Union

    for tp in (int | None, int | str, Union[int, str], Optional[int]):
        FieldModel(base_type=tp)  # must not raise


def test_to_spec_retains_unknown_metadata():
    """Metadata keys outside the known set survive to_spec()."""
    spec = FieldModel(base_type=int, custom_meta="kept").to_spec()
    assert spec.get("custom_meta") == "kept"


def test_to_spec_preserves_explicit_none_default():
    """An explicit default=None must survive to_spec() (not treated as absent)."""
    spec = FieldModel(base_type=int, default=None).to_spec()
    assert spec.default is None


def test_to_spec_normalizes_legacy_none_annotation_to_unset():
    """FieldModel owns its legacy None-as-unspecified adapter behavior."""
    spec = FieldModel(annotation=None).to_spec()

    assert spec.base_type is Unset


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"base_type": None},
        {"base_type": Undefined},
        {"base_type": Unset},
        {"annotation": None},
    ],
)
def test_to_spec_preserves_the_legacy_unspecified_base_type_matrix(kwargs):
    spec = FieldModel(**kwargs).to_spec()

    assert spec.base_type is Unset


def test_to_spec_does_not_flatten_json_schema_extra():
    """A 'default' key inside json_schema_extra must not become the runtime default."""
    spec = FieldModel(base_type=int, default=5, json_schema_extra={"default": 999}).to_spec()
    assert spec.default == 5
    assert spec.get("json_schema_extra") == {"default": 999}


@pytest.mark.parametrize("key", ["self", "base_type", "metadata"])
def test_to_spec_forwards_reserved_metadata_keys(key):
    """Metadata keys that collide with Spec.__init__ parameters still round-trip.

    Passing metadata as **kwargs would raise (multiple-values / str-iteration);
    forwarding it as a Meta tuple keeps these keys intact.
    """
    spec = FieldModel(base_type=int).with_metadata(key, "kept").to_spec()
    assert spec.get(key) == "kept"
