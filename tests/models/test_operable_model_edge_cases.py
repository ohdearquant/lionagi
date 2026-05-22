"""Edge case tests for operable_model.py."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from lionagi.models.field_model import FieldModel
from lionagi.models.operable_model import OperableModel


def _pos_validator(v):
    return v > 0


class TestOperableModelAddField:
    def test_add_field_basic(self):
        m = OperableModel()
        m.add_field("score", value=10, annotation=int)
        assert m.score == 10
        assert "score" in m.extra_fields

    def test_add_field_string_annotation(self):
        m = OperableModel()
        m.add_field("label", value="hello", annotation=str)
        assert m.label == "hello"

    def test_add_field_duplicate_raises(self):
        m = OperableModel()
        m.add_field("x", value=1, annotation=int)
        with pytest.raises(ValueError, match="already exists"):
            m.add_field("x", value=2, annotation=int)

    def test_add_field_with_field_model(self):
        m = OperableModel()
        fm = FieldModel(base_type=float, description="rate")
        m.add_field("rate", value=3.14, field_model=fm)
        assert m.rate == 3.14
        assert "rate" in m.extra_field_models

    def test_add_field_no_value(self):
        m = OperableModel()
        m.add_field("empty_field", annotation=str)
        assert "empty_field" in m.extra_fields

    def test_add_field_appears_in_all_fields(self):
        m = OperableModel()
        m.add_field("foo", value=42, annotation=int)
        assert "foo" in m.all_fields


class TestOperableModelUpdateField:
    def test_update_field_value(self):
        m = OperableModel()
        m.add_field("count", value=1, annotation=int)
        m.update_field("count", value=99)
        assert m.count == 99

    def test_update_field_creates_if_not_exists(self):
        m = OperableModel()
        m.update_field("new_field", value="v", annotation=str)
        assert m.new_field == "v"

    def test_update_field_both_default_and_factory_raises(self):
        m = OperableModel()
        with pytest.raises(ValueError, match="both"):
            m.update_field("x", default=1, default_factory=list)

    def test_update_field_both_field_obj_and_model_raises(self):
        from pydantic import Field

        m = OperableModel()
        fi = Field(default=1)
        fm = FieldModel(base_type=int)
        with pytest.raises(ValueError, match="both"):
            m.update_field("y", field_obj=fi, field_model=fm)

    def test_update_field_invalid_field_obj_raises(self):
        m = OperableModel()
        with pytest.raises(ValueError, match="FieldInfo"):
            m.update_field("z", field_obj="not_a_field_info")

    def test_update_field_invalid_field_model_raises(self):
        m = OperableModel()
        with pytest.raises(ValueError, match="FieldModel"):
            m.update_field("z", field_model="not_a_field_model")


class TestOperableModelRemoveField:
    def test_remove_field_removes_from_extra_fields(self):
        m = OperableModel()
        m.add_field("temp", value=5, annotation=int)
        assert "temp" in m.extra_fields
        m.remove_field("temp")
        assert "temp" not in m.extra_fields

    def test_remove_field_removes_value_from_dict(self):
        m = OperableModel()
        m.add_field("tmp2", value=99, annotation=int)
        m.remove_field("tmp2")
        assert m.__dict__.get("tmp2") is None

    def test_remove_nonexistent_field_noop(self):
        m = OperableModel()
        m.add_field("existing", value=1, annotation=int)
        m.remove_field("does_not_exist")
        assert "existing" in m.all_fields


class TestOperableModelFieldAttr:
    def test_field_getattr_description(self):
        m = OperableModel()
        fm = FieldModel(base_type=str, description="test desc")
        m.add_field("labeled", value="x", field_model=fm)
        desc = m.field_getattr("labeled", "description")
        assert desc == "test desc"

    def test_field_getattr_missing_field_raises_key_error(self):
        m = OperableModel()
        with pytest.raises(KeyError):
            m.field_getattr("nonexistent", "description")

    def test_field_getattr_missing_attr_returns_default(self):
        m = OperableModel()
        m.add_field("n", value=1, annotation=int)
        result = m.field_getattr("n", "nonexistent_attr", "fallback")
        assert result == "fallback"

    def test_field_getattr_missing_attr_no_default_raises(self):
        m = OperableModel()
        m.add_field("n2", value=1, annotation=int)
        with pytest.raises(AttributeError):
            m.field_getattr("n2", "totally_missing_attr")

    def test_field_setattr_description(self):
        m = OperableModel()
        m.add_field("item", value="v", annotation=str)
        m.field_setattr("item", "description", "new desc")
        desc = m.field_getattr("item", "description", None)
        assert desc is not None

    def test_field_setattr_missing_field_raises_key_error(self):
        m = OperableModel()
        with pytest.raises(KeyError):
            m.field_setattr("ghost", "description", "x")

    def test_field_hasattr_existing_attr(self):
        m = OperableModel()
        m.add_field("chk", value=1, annotation=int)
        assert m.field_hasattr("chk", "annotation") is True

    def test_field_hasattr_missing_field_raises_key_error(self):
        m = OperableModel()
        with pytest.raises(KeyError):
            m.field_hasattr("missing", "annotation")


class TestOperableModelNewModel:
    def test_new_model_returns_type(self):
        m = OperableModel()
        m.add_field("name", value="Alice", annotation=str)
        NewCls = m.new_model("Person")
        assert isinstance(NewCls, type)
        assert issubclass(NewCls, BaseModel)

    def test_new_model_has_specified_name(self):
        m = OperableModel()
        m.add_field("x", value=1, annotation=int)
        Cls = m.new_model("MyDynamic")
        assert Cls.__name__ == "MyDynamic"

    def test_new_model_instantiable(self):
        m = OperableModel()
        m.add_field("score", value=0, annotation=int)
        Cls = m.new_model("ScoreModel", use_fields={"score"})
        instance = Cls(score=42)
        assert instance.score == 42

    def test_new_model_invalid_fields_raises(self):
        m = OperableModel()
        m.add_field("a", value=1, annotation=int)
        with pytest.raises(ValueError, match="Invalid field"):
            m.new_model("Bad", use_fields={"nonexistent_field"})

    def test_new_model_frozen(self):
        m = OperableModel()
        m.add_field("val", value=1, annotation=int)
        FrozenCls = m.new_model("Frozen", use_fields={"val"}, frozen=True)
        instance = FrozenCls(val=5)
        with pytest.raises(ValidationError):
            instance.val = 10

    def test_new_model_without_name(self):
        m = OperableModel()
        Cls = m.new_model()
        assert isinstance(Cls, type)


class TestOperableModelSerialize:
    def test_model_dump_includes_extra_fields(self):
        m = OperableModel()
        m.add_field("points", value=5, annotation=int)
        td = m.to_dict()
        assert "points" in td

    def test_to_dict_excludes_undefined(self):
        from lionagi.utils import UNDEFINED

        m = OperableModel()
        m.add_field("maybe", annotation=str)
        d = m.to_dict()
        assert d.get("maybe") is not UNDEFINED

    def test_all_fields_excludes_internal(self):
        m = OperableModel()
        m.add_field("real_field", value=1, annotation=int)
        af = m.all_fields
        assert "extra_fields" not in af
        assert "extra_field_models" not in af
        assert "real_field" in af


class TestOperableModelSetAttr:
    def test_setattr_with_validator_pass(self):
        m = OperableModel()
        fm = FieldModel(base_type=int).with_validator(_pos_validator)
        m.add_field("positive", value=1, field_model=fm)
        m.positive = 5
        assert m.positive == 5

    def test_dunder_field_assignment_raises(self):
        m = OperableModel()
        with pytest.raises(AttributeError):
            m.__dunder__ = "bad"


class TestOperableModelDelAttr:
    def test_delattr_extra_field_with_no_default(self):
        m = OperableModel()
        m.add_field("ephemeral", value=42, annotation=int)
        with pytest.raises(TypeError):
            del m.ephemeral

    def test_delattr_extra_field_with_default_resets(self):
        from pydantic import Field

        m = OperableModel()
        fi = Field(default=0)
        fi.annotation = int
        m.extra_fields["resettable"] = fi
        object.__setattr__(m, "resettable", 99)
        del m.resettable
        assert m.resettable == 0
