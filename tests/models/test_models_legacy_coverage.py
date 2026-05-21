"""Coverage tests for FieldModel and OperableModel."""

import warnings
from typing import Any

import pytest
from pydantic.fields import FieldInfo

from lionagi.models.field_model import FieldModel
from lionagi.models.operable_model import OperableModel

# ---------------------------------------------------------------------------
# FieldModel tests
# ---------------------------------------------------------------------------


class TestFieldModelInit:
    def test_basic_init_with_base_type(self):
        f = FieldModel(base_type=int)
        assert not f._is_sentinel(f.base_type)
        assert f.base_type is int

    def test_annotation_alias_for_base_type(self):
        f = FieldModel(annotation=str)
        assert f.base_type is str

    def test_name_stored_in_metadata(self):
        f = FieldModel(base_type=int, name="score")
        assert f.name == "score"

    def test_default_name_is_field(self):
        f = FieldModel(base_type=int)
        assert f.name == "field"

    def test_no_args_init(self):
        f = FieldModel()
        assert f._is_sentinel(f.base_type)

    def test_default_value_in_metadata(self):
        f = FieldModel(base_type=int, default=42)
        assert f.extract_metadata("default") == 42

    def test_default_factory_in_metadata(self):
        f = FieldModel(base_type=list, default_factory=list)
        assert f.extract_metadata("default_factory") is list

    def test_conflicting_default_and_factory_raises(self):
        with pytest.raises(ValueError, match="default"):
            FieldModel(base_type=int, default=1, default_factory=list)

    def test_validator_callable_accepted(self):
        def is_positive(v):
            return v > 0

        f = FieldModel(base_type=int, validator=is_positive)
        assert f.has_validator()

    def test_validator_non_callable_raises(self):
        with pytest.raises(ValueError):
            FieldModel(base_type=int, validator="not_callable")

    def test_unknown_kwarg_stored_as_metadata(self):
        # FieldModel's legacy API is permissive: unknown kwargs become Meta entries.
        f = FieldModel(base_type=int, nonexistent_kwarg=True)
        assert f.extract_metadata("nonexistent_kwarg") is True


class TestFieldModelProperties:
    def test_annotation_property_returns_base_type(self):
        f = FieldModel(base_type=float)
        assert f.annotation is float

    def test_annotation_property_any_when_unset(self):

        f = FieldModel()
        assert f.annotation is Any

    def test_is_nullable_false_by_default(self):
        f = FieldModel(base_type=str)
        assert not f.is_nullable

    def test_is_nullable_true_when_set(self):
        f = FieldModel(base_type=str, nullable=True)
        assert f.is_nullable

    def test_is_listable_false_by_default(self):
        f = FieldModel(base_type=str)
        assert not f.is_listable

    def test_is_listable_true_when_set(self):
        f = FieldModel(base_type=str, listable=True)
        assert f.is_listable

    def test_getattr_returns_metadata_value(self):
        f = FieldModel(base_type=int, description="a number")
        assert f.description == "a number"

    def test_getattr_missing_raises_attribute_error(self):
        f = FieldModel(base_type=int)
        with pytest.raises(AttributeError):
            _ = f.does_not_exist

    def test_repr_includes_type_name(self):
        f = FieldModel(base_type=int)
        assert "int" in repr(f)

    def test_repr_shows_nullable_flag(self):
        f = FieldModel(base_type=int, nullable=True)
        assert "nullable" in repr(f)

    def test_repr_shows_validated_flag(self):
        f = FieldModel(base_type=int, validator=lambda v: True)
        assert "validated" in repr(f)


class TestFieldModelMethods:
    def test_create_field_returns_field_info(self):
        f = FieldModel(base_type=str, default="hello")
        fi = f.create_field()
        assert isinstance(fi, FieldInfo)

    def test_create_field_sets_annotation(self):
        f = FieldModel(base_type=int)
        fi = f.create_field()
        assert fi.annotation is int

    def test_to_dict_deprecated_but_works(self):
        f = FieldModel(base_type=int, description="x")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            d = f.to_dict()
            assert any(
                issubclass(warning.category, DeprecationWarning) for warning in w
            )
        assert isinstance(d, dict)

    def test_metadata_dict_returns_dict(self):
        f = FieldModel(base_type=int, description="test")
        d = f.metadata_dict()
        assert isinstance(d, dict)
        assert "description" in d

    def test_metadata_dict_exclude(self):
        f = FieldModel(base_type=int, description="test", name="x")
        d = f.metadata_dict(exclude=["name"])
        assert "name" not in d

    def test_extract_metadata_returns_value(self):
        f = FieldModel(base_type=int, default=99)
        assert f.extract_metadata("default") == 99

    def test_extract_metadata_returns_none_if_missing(self):
        f = FieldModel(base_type=int)
        assert f.extract_metadata("nonexistent") is None

    def test_annotated_returns_type(self):
        f = FieldModel(base_type=str)
        a = f.annotated()
        assert a is str

    def test_annotated_caches_result(self):
        f = FieldModel(base_type=str)
        assert f.annotated() is f.annotated()

    def test_has_validator_false_when_no_validator(self):
        f = FieldModel(base_type=int)
        assert not f.has_validator()

    def test_has_validator_true_when_set(self):
        f = FieldModel(base_type=int, validator=lambda v: True)
        assert f.has_validator()

    def test_is_valid_true_when_no_validators(self):
        f = FieldModel(base_type=int)
        assert f.is_valid(42)

    def test_is_valid_uses_validator(self):
        f = FieldModel(base_type=int, validator=lambda v: v > 0)
        assert f.is_valid(5)
        assert not f.is_valid(-1)


class TestFieldModelFactoryHelpers:
    def test_as_nullable_creates_nullable(self):
        f = FieldModel(base_type=int)
        fn = f.as_nullable()
        assert fn.is_nullable
        assert not f.is_nullable  # original unchanged

    def test_as_listable_creates_listable(self):
        f = FieldModel(base_type=str)
        fl = f.as_listable()
        assert fl.is_listable

    def test_with_validator_adds_validator(self):
        f = FieldModel(base_type=int)
        fv = f.with_validator(lambda v: v > 0)
        assert fv.has_validator()
        assert not f.has_validator()

    def test_with_description_adds_description(self):
        f = FieldModel(base_type=str)
        fd = f.with_description("a description")
        assert fd.extract_metadata("description") == "a description"

    def test_with_description_replaces_existing(self):
        f = FieldModel(base_type=str).with_description("old")
        fd = f.with_description("new")
        assert fd.extract_metadata("description") == "new"

    def test_with_default_adds_default(self):
        f = FieldModel(base_type=int)
        fd = f.with_default(0)
        assert fd.extract_metadata("default") == 0

    def test_with_frozen_adds_frozen(self):
        f = FieldModel(base_type=int)
        ff = f.with_frozen(True)
        assert ff.extract_metadata("frozen") is True

    def test_with_title_adds_title(self):
        f = FieldModel(base_type=str)
        ft = f.with_title("My Title")
        assert ft.extract_metadata("title") == "My Title"

    def test_with_alias_adds_alias(self):
        f = FieldModel(base_type=str, name="x")
        fa = f.with_alias("x_alias")
        assert fa.extract_metadata("alias") == "x_alias"

    def test_with_exclude_marks_exclude(self):
        f = FieldModel(base_type=str)
        fe = f.with_exclude(True)
        assert fe.extract_metadata("exclude") is True

    def test_chaining_helpers(self):
        f = (
            FieldModel(base_type=int)
            .as_nullable()
            .with_description("an int")
            .with_default(0)
        )
        assert f.is_nullable
        assert f.extract_metadata("description") == "an int"
        assert f.extract_metadata("default") == 0

    def test_nullable_annotation_includes_none(self):
        f = FieldModel(base_type=int).as_nullable()

        ann = f.annotation
        # str | None or Optional[int]
        assert ann is not int  # should be a union type


# ---------------------------------------------------------------------------
# OperableModel tests
# ---------------------------------------------------------------------------


class SimpleModel(OperableModel):
    value: int = 0
    label: str = "default"


class TestOperableModelInit:
    def test_basic_subclass_init(self):
        m = SimpleModel()
        assert m.value == 0
        assert m.label == "default"

    def test_subclass_init_with_values(self):
        m = SimpleModel(value=42, label="hello")
        assert m.value == 42
        assert m.label == "hello"

    def test_extra_fields_is_empty_dict_initially(self):
        m = SimpleModel()
        assert isinstance(m.extra_fields, dict)
        assert len(m.extra_fields) == 0


class TestOperableModelAddField:
    def test_add_field_creates_new_field(self):
        m = SimpleModel()
        m.add_field("score", annotation=int, value=10)
        assert "score" in m.extra_fields

    def test_add_field_value_accessible(self):
        m = SimpleModel()
        m.add_field("score", annotation=int, value=99)
        assert m.score == 99

    def test_add_field_with_default(self):
        m = SimpleModel()
        m.add_field("tag", annotation=str, default="foo")
        assert m.tag == "foo"

    def test_add_field_duplicate_raises(self):
        m = SimpleModel()
        m.add_field("score", annotation=int, value=1)
        with pytest.raises(ValueError, match="already exists"):
            m.add_field("score", annotation=int, value=2)

    def test_add_existing_model_field_raises(self):
        m = SimpleModel()
        with pytest.raises(ValueError, match="already exists"):
            m.add_field("value", annotation=int, value=5)

    def test_all_fields_includes_new_field(self):
        m = SimpleModel()
        m.add_field("extra", annotation=float, value=1.5)
        assert "extra" in m.all_fields


class TestOperableModelUpdateField:
    def test_update_field_changes_existing_extra_field(self):
        m = SimpleModel()
        m.add_field("count", annotation=int, value=0)
        m.update_field("count", value=10)
        assert m.count == 10

    def test_update_field_conflicting_defaults_raises(self):
        m = SimpleModel()
        m.add_field("x", annotation=int, value=0)
        with pytest.raises(ValueError):
            m.update_field("x", default=1, default_factory=list)

    def test_field_setattr_updates_field_attribute(self):
        m = SimpleModel()
        m.add_field("n", annotation=int, value=5)
        m.field_setattr("n", "description", "a number")
        fi = m.extra_fields["n"]
        # Known FieldInfo attributes are set directly on the FieldInfo;
        # only unknown attrs get routed into json_schema_extra.
        assert fi.description == "a number"
        m.field_setattr("n", "custom_attr", "custom_value")
        assert fi.json_schema_extra is not None
        assert fi.json_schema_extra.get("custom_attr") == "custom_value"

    def test_field_getattr_returns_value(self):
        m = SimpleModel()
        m.add_field("n", annotation=int, value=5)
        m.field_setattr("n", "description", "x")
        val = m.field_getattr("n", "description")
        assert val == "x"

    def test_field_getattr_missing_with_default(self):
        m = SimpleModel()
        m.add_field("n", annotation=int, value=5)
        val = m.field_getattr("n", "missing_attr", "fallback")
        assert val == "fallback"

    def test_field_hasattr_returns_true_for_known(self):
        m = SimpleModel()
        m.add_field("n", annotation=int, value=5)
        # FieldInfo has 'default' attribute
        assert m.field_hasattr("n", "default")

    def test_field_hasattr_missing_field_raises(self):
        m = SimpleModel()
        with pytest.raises(KeyError):
            m.field_hasattr("nonexistent", "default")


class TestOperableModelRemoveField:
    def test_remove_field_removes_from_extra_fields(self):
        m = SimpleModel()
        m.add_field("temp", annotation=str, value="hi")
        m.remove_field("temp")
        assert "temp" not in m.extra_fields

    def test_remove_field_removes_from_dict(self):
        m = SimpleModel()
        m.add_field("temp", annotation=str, value="hi")
        m.remove_field("temp")
        assert "temp" not in m.__dict__

    def test_remove_nonexistent_field_is_safe(self):
        m = SimpleModel()
        m.add_field("existing", value="v", annotation=str)
        m.remove_field("nonexistent")
        assert "existing" in m.all_fields


class TestOperableModelAllFields:
    def test_all_fields_excludes_extra_fields_key(self):
        m = SimpleModel()
        assert "extra_fields" not in m.all_fields

    def test_all_fields_excludes_extra_field_models_key(self):
        m = SimpleModel()
        assert "extra_field_models" not in m.all_fields

    def test_all_fields_includes_model_fields(self):
        m = SimpleModel()
        assert "value" in m.all_fields
        assert "label" in m.all_fields


class TestOperableModelSerialization:
    def test_to_dict_returns_dict(self):
        m = SimpleModel(value=5, label="x")
        d = m.to_dict()
        assert isinstance(d, dict)
        assert d["value"] == 5
        assert d["label"] == "x"

    def test_to_dict_includes_extra_fields(self):
        m = SimpleModel()
        m.add_field("bonus", annotation=int, value=99)
        d = m.to_dict()
        assert d.get("bonus") == 99

    def test_from_dict_roundtrip(self):
        data = {"value": 7, "label": "test"}
        m = SimpleModel.from_dict(data)
        assert m.value == 7
        assert m.label == "test"

    def test_to_json_produces_string(self):
        m = SimpleModel(value=1, label="a")
        j = m.to_json()
        assert isinstance(j, str)
        assert "value" in j

    def test_model_copy_preserves_values(self):
        m = SimpleModel(value=3, label="orig")
        c = m.model_copy()
        assert c.value == 3
        assert c.label == "orig"

    def test_model_copy_with_update(self):
        m = SimpleModel(value=3, label="orig")
        c = m.model_copy(update={"value": 99})
        assert c.value == 99
        assert m.value == 3  # original unchanged


class TestOperableModelConfig:
    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            SimpleModel(nonexistent_field="oops")

    def test_model_fields_structure(self):
        fields = SimpleModel.model_fields
        assert "value" in fields
        assert "label" in fields


class TestOperableModelNewModel:
    def test_new_model_creates_class(self):
        m = SimpleModel()
        m.add_field("age", annotation=int, value=25)
        NewCls = m.new_model(name="DynamicModel", use_fields={"value", "age"})
        assert isinstance(NewCls, type)

    def test_new_model_invalid_fields_raises(self):
        m = SimpleModel()
        with pytest.raises(ValueError):
            m.new_model(use_fields={"nonexistent_field"})

    def test_new_model_can_instantiate(self):
        m = SimpleModel()
        m.add_field("age", annotation=int, value=25)
        NewCls = m.new_model(name="DynamicModel", use_fields={"value", "age"})
        instance = NewCls(value=10, age=30)
        assert instance.value == 10
        assert instance.age == 30
