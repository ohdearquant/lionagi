# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Extra coverage tests for OperableModel targeting uncovered branches."""

import pytest
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from lionagi.models.field_model import FieldModel
from lionagi.models.operable_model import OperableModel


class _Sample(OperableModel):
    base: str = "x"


class TestSerializeExtraFields:
    def test_extra_field_with_model_dump_is_dumped(self):
        class Inner(BaseModel):
            v: int = 1

        m = _Sample()
        m.add_field("inner", value=Inner(v=5), annotation=Inner)
        d = m.to_dict()
        assert d["inner"] == {"v": 5}

    def test_extra_field_with_to_dict_is_dumped(self):
        class CustomObj:
            def to_dict(self):
                return {"kind": "custom"}

        m = _Sample()
        m.add_field("obj", value=CustomObj(), annotation=CustomObj)
        d = m.to_dict()
        assert d["obj"] == {"kind": "custom"}

    def test_plain_extra_field_passthrough(self):
        m = _Sample()
        m.add_field("n", value=7, annotation=int)
        d = m.to_dict()
        assert d["n"] == 7


class TestValidateExtraFieldsClassmethod:
    def test_init_with_dict_of_field_models(self):
        fm = FieldModel(name="score", base_type=int)
        m = _Sample(extra_fields={"score": fm})
        assert "score" in m.extra_fields
        assert isinstance(m.extra_fields["score"], FieldInfo)

    def test_init_with_dict_of_field_info(self):
        fi = Field(default=0)
        m = _Sample(extra_fields={"s": fi})
        assert "s" in m.extra_fields

    def test_init_with_list_of_field_models(self):
        fms = [FieldModel(name="a", base_type=int), FieldModel(name="b", base_type=str)]
        m = _Sample(extra_fields=fms)
        assert "a" in m.extra_fields
        assert "b" in m.extra_fields

    def test_init_with_invalid_extra_fields_raises(self):
        with pytest.raises(ValueError):
            _Sample(extra_fields=42)


class TestSetattrRegularField:
    def test_assign_regular_field_delegates_to_super(self):
        m = _Sample()
        m.base = "changed"
        assert m.base == "changed"

    def test_dunder_assignment_rejected(self):
        m = _Sample()
        with pytest.raises(AttributeError, match="dunder"):
            m.__something__ = 1


class TestDelattrRegularField:
    def test_delete_regular_field_uses_super(self):
        m = _Sample()
        m.base = "changed"
        assert m.base == "changed"
        del m.base
        # After deletion the field value is cleared to the Undefined sentinel
        from lionagi.ln.types._sentinel import UndefinedType

        assert isinstance(m.base, UndefinedType)


class TestAddFieldEdgeCases:
    def test_add_field_conflicting_default_factory(self):
        m = _Sample()
        with pytest.raises(ValueError, match="Cannot provide both"):
            m.add_field("x", default=1, default_factory=list)

    def test_add_field_with_both_field_obj_and_field_model_raises(self):
        m = _Sample()
        fi = Field(default=0)
        fm = FieldModel(name="x", base_type=int)
        with pytest.raises(ValueError, match="Cannot provide both"):
            m.add_field("x", field_obj=fi, field_model=fm)

    def test_add_field_invalid_field_obj(self):
        m = _Sample()
        with pytest.raises(ValueError, match="pydantic FieldInfo"):
            m.add_field("x", field_obj="not-a-fieldinfo")

    def test_add_field_invalid_field_model(self):
        m = _Sample()
        with pytest.raises(ValueError, match="FieldModel object"):
            m.add_field("x", field_model="not-a-fm")

    def test_add_field_with_field_obj(self):
        m = _Sample()
        fi = Field(default=42)
        m.add_field("num", field_obj=fi)
        assert m.num == 42

    def test_add_field_with_field_model(self):
        m = _Sample()
        fm = FieldModel(name="num", base_type=int, default=3)
        m.add_field("num", field_model=fm)
        assert "num" in m.extra_field_models

    def test_add_field_with_default_factory_resolves_value(self):
        m = _Sample()
        m.add_field("items", annotation=list, default_factory=list)
        assert m.items == []


class TestFieldHasattr:
    def test_known_fieldinfo_attr(self):
        m = _Sample()
        m.add_field("n", annotation=int, value=1)
        assert m.field_hasattr("n", "annotation") is True

    def test_custom_json_schema_extra_attr(self):
        m = _Sample()
        m.add_field("n", annotation=int, value=1)
        m.field_setattr("n", "custom_key", "val")
        # field_hasattr must check for the *attr* key, not the field name.
        # "custom_key" was stored in json_schema_extra — it must be found.
        assert m.field_hasattr("n", "custom_key") is True
        # "nothing" was never stored — it must NOT be found (old bug: returned True).
        assert not m.field_hasattr("n", "nothing")

    def test_missing_field_raises(self):
        m = _Sample()
        with pytest.raises(KeyError):
            m.field_hasattr("missing", "x")


class TestFieldGetattr:
    def test_annotation_shortcut(self):
        m = _Sample()
        # 'annotation' (singular) and 'annotations' both map via strip("s")
        ann = m.field_getattr("base", "annotation")
        assert ann is str

    def test_missing_field_raises(self):
        m = _Sample()
        with pytest.raises(KeyError):
            m.field_getattr("ghost", "x")

    def test_no_default_raises_attribute(self):
        m = _Sample()
        m.add_field("n", annotation=int, value=1)
        with pytest.raises(AttributeError, match="no attribute"):
            m.field_getattr("n", "unknown_attr")


class TestNewModel:
    def test_new_model_with_extra_field_models(self):
        m = _Sample()
        fm = FieldModel(name="score", base_type=int, default=0)
        m.add_field("score", field_model=fm)
        NewCls = m.new_model(name="Scored", use_fields={"score"}, inherit_base=False)
        assert issubclass(NewCls, BaseModel)
        inst = NewCls(score=3)
        assert inst.score == 3

    def test_new_model_invalid_use_fields_raises(self):
        m = _Sample()
        with pytest.raises(ValueError, match="Invalid field names"):
            m.new_model(use_fields={"nonexistent"})

    def test_new_model_rebuild_failure_swallowed(self, monkeypatch):
        m = _Sample()

        # Force model_rebuild to raise — the except clause must swallow it.
        class _Boom(BaseModel):
            @classmethod
            def model_rebuild(cls, *a, **kw):
                raise RuntimeError("forward ref broken")

        # Patch ModelParams.create_new_model to return our Boom class.
        from lionagi.models import operable_model as om

        monkeypatch.setattr(om.ModelParams, "create_new_model", lambda self: _Boom)
        # Should NOT raise.
        cls = m.new_model(use_fields={"base"}, update_forward_refs=True)
        assert cls is _Boom
