# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import pytest
from pydantic import BaseModel

from lionagi.lndl.types import (
    ActionCall,
    LactMetadata,
    LNDLOutput,
    LvarMetadata,
    ParsedConstructor,
    RLvarMetadata,
    _coerce_result,
    _revalidate_model,
    ensure_no_action_calls,
    has_action_calls,
    revalidate_with_action_results,
)


class SimpleModel(BaseModel):
    title: str
    score: float = 0.0


class NestedModel(BaseModel):
    name: str
    inner: SimpleModel | None = None


class OptionalModel(BaseModel):
    note: str | None = None
    count: int | None = None


def make_action_call(name="act1", fn="fetch", args=None):
    return ActionCall(
        name=name,
        function=fn,
        arguments=args or {"url": "http://x"},
        raw_call=f"{fn}(url='http://x')",
    )


class TestLvarMetadata:
    def test_frozen(self):
        m = LvarMetadata(model="R", field="f", local_name="l", value="v")
        with pytest.raises((AttributeError, TypeError)):
            m.model = "X"


class TestRLvarMetadata:
    def test_frozen(self):
        m = RLvarMetadata(local_name="x", value="v")
        with pytest.raises((AttributeError, TypeError)):
            m.local_name = "y"


class TestLactMetadata:
    def test_with_model_and_field(self):
        m = LactMetadata(model="Report", field="summary", local_name="s", call="fn(a='b')")
        assert m.model == "Report"
        assert m.field == "summary"

    def test_direct_lact(self):
        m = LactMetadata(model=None, field=None, local_name="data", call="fetch(url='x')")
        assert m.model is None
        assert m.field is None


class TestParsedConstructor:
    def test_has_dict_unpack_false(self):
        pc = ParsedConstructor(class_name="A", kwargs={"x": 1}, raw="A(x=1)")
        assert pc.has_dict_unpack is False

    def test_has_dict_unpack_true(self):
        pc = ParsedConstructor(class_name="A", kwargs={"**data": {}}, raw="A(**data)")
        assert pc.has_dict_unpack is True


class TestActionCall:
    def test_frozen(self):
        ac = make_action_call()
        with pytest.raises((AttributeError, TypeError)):
            ac.name = "other"


class TestLNDLOutput:
    def test_getitem(self):
        ac = make_action_call()
        out = LNDLOutput(
            fields={"score": 0.9, "report": ac},
            lvars={},
            lacts={},
            actions={"act1": ac},
            raw_out_block="OUT{...}",
        )
        assert out["score"] == pytest.approx(0.9)
        assert out["report"] is ac

    def test_getattr_delegates_to_fields(self):
        out = LNDLOutput(
            fields={"score": 0.9},
            lvars={},
            lacts={},
            actions={},
            raw_out_block="",
        )
        assert out.score == pytest.approx(0.9)

    def test_getattr_own_fields(self):
        out = LNDLOutput(
            fields={"score": 0.9},
            lvars={"x": RLvarMetadata(local_name="x", value="v")},
            lacts={},
            actions={},
            raw_out_block="test",
        )
        assert out.raw_out_block == "test"
        assert "x" in out.lvars


class TestHasActionCalls:
    def test_clean_model(self):
        m = SimpleModel(title="hello", score=1.0)
        assert has_action_calls(m) is False

    def test_model_with_action_call(self):
        ac = make_action_call()
        m = SimpleModel.model_construct(title=ac, score=0.0)
        assert has_action_calls(m) is True

    def test_nested_model_with_action_call(self):
        ac = make_action_call()
        inner = SimpleModel.model_construct(title=ac, score=0.0)
        outer = NestedModel(name="outer", inner=inner)
        assert has_action_calls(outer) is True

    def test_clean_nested(self):
        inner = SimpleModel(title="x", score=1.0)
        outer = NestedModel(name="outer", inner=inner)
        assert has_action_calls(outer) is False


class TestEnsureNoActionCalls:
    def test_clean_model_passthrough(self):
        m = SimpleModel(title="hello", score=1.0)
        result = ensure_no_action_calls(m)
        assert result is m

    def test_model_with_action_raises(self):
        ac = make_action_call()
        m = SimpleModel.model_construct(title=ac, score=0.0)
        with pytest.raises(ValueError, match="unexecuted actions"):
            ensure_no_action_calls(m)

    def test_error_message_includes_field_name(self):
        ac = make_action_call()
        m = SimpleModel.model_construct(title=ac, score=0.0)
        with pytest.raises(ValueError) as exc_info:
            ensure_no_action_calls(m)
        assert "title" in str(exc_info.value)


class TestCoerceResult:
    def test_dict_to_str_for_str_target(self):
        result = _coerce_result({"key": "val"}, str)
        assert isinstance(result, str)

    def test_int_coercion(self):
        result = _coerce_result("42", int)
        assert result == 42

    def test_float_coercion(self):
        result = _coerce_result("3.14", float)
        assert result == pytest.approx(3.14)

    def test_no_coercion_same_type(self):
        result = _coerce_result("hello", str)
        assert result == "hello"

    def test_none_target_type(self):
        result = _coerce_result({"x": 1}, None)
        assert result == {"x": 1}


class TestRevalidateModel:
    def test_replaces_action_call(self):
        ac = ActionCall(name="t", function="fetch", arguments={}, raw_call="fetch()")
        m = SimpleModel.model_construct(title=ac, score=1.0)
        action_results = {"t": "Fetched Title"}
        result = _revalidate_model(m, action_results)
        assert result.title == "Fetched Title"

    def test_raises_when_result_missing(self):
        ac = ActionCall(name="missing", function="fn", arguments={}, raw_call="fn()")
        m = SimpleModel.model_construct(title=ac, score=1.0)
        with pytest.raises(ValueError, match="no execution result"):
            _revalidate_model(m, {})

    def test_unchanged_model_returns_same(self):
        m = SimpleModel(title="hello", score=1.0)
        result = _revalidate_model(m, {})
        assert result is m


class TestRevalidateWithActionResults:
    def test_full_revalidation(self):
        ac = ActionCall(name="s", function="summarize", arguments={}, raw_call="summarize()")
        m = SimpleModel.model_construct(title="My Title", score=ac)
        result = revalidate_with_action_results(m, {"s": 0.95})
        assert result.score == pytest.approx(0.95)
        assert result.title == "My Title"


# ---------------------------------------------------------------------------
# Regression: LNDLOutput.__getattr__ must raise AttributeError not KeyError
# ---------------------------------------------------------------------------


class TestLNDLOutputGetAttrRaisesAttributeError:
    """LIONAGI-AUDIT-001 (lndl): missing dynamic attrs must raise AttributeError."""

    def make_empty_output(self):
        return LNDLOutput(fields={}, lvars={}, lacts={}, actions={}, raw_out_block="")

    def test_missing_attr_raises_attribute_error(self):
        """out.missing must raise AttributeError so hasattr() works correctly."""
        out = self.make_empty_output()
        with pytest.raises(AttributeError):
            _ = out.missing_field

    def test_hasattr_returns_false_for_missing(self):
        """hasattr() must return False for absent dynamic attributes."""
        out = self.make_empty_output()
        assert hasattr(out, "missing_field") is False

    def test_getattr_default_works_for_missing(self):
        """getattr(out, 'missing', default) must return the default."""
        out = self.make_empty_output()
        sentinel = object()
        result = getattr(out, "missing_field", sentinel)
        assert result is sentinel

    def test_present_attr_still_accessible(self):
        """Fields that exist are still accessible through __getattr__."""
        out = LNDLOutput(
            fields={"score": 0.5},
            lvars={},
            lacts={},
            actions={},
            raw_out_block="",
        )
        assert out.score == pytest.approx(0.5)

    def test_own_attr_not_affected(self):
        """Structural attributes (fields, lvars, etc.) bypass dynamic lookup."""
        out = LNDLOutput(
            fields={"x": 1},
            lvars={"k": "v"},
            lacts={},
            actions={},
            raw_out_block="test",
        )
        assert out.raw_out_block == "test"
        assert out.fields == {"x": 1}


# ---------------------------------------------------------------------------
# Regression: _coerce_result handles Optional scalar annotations
# ---------------------------------------------------------------------------


class TestCoerceResultOptionalScalar:
    """LIONAGI-AUDIT-002 (lndl): optional scalar fields must coerce like required ones."""

    def test_optional_str_dict_result(self):
        """str | None target: dict result must be JSON-serialised to str."""
        result = _coerce_result({"key": "val"}, str | None)
        assert isinstance(result, str)

    def test_optional_int_str_result(self):
        """int | None target: str '42' must be coerced to int 42."""
        result = _coerce_result("42", int | None)
        assert result == 42
        assert isinstance(result, int)

    def test_optional_float_str_result(self):
        """float | None target: str '3.14' must be coerced to float."""
        result = _coerce_result("3.14", float | None)
        assert isinstance(result, float)
        assert abs(result - 3.14) < 1e-6

    def test_optional_str_already_correct_type(self):
        """str | None target when result is already str — no change."""
        result = _coerce_result("hello", str | None)
        assert result == "hello"

    def test_required_str_dict_still_works(self):
        """Regression: required str target still serialises dict."""
        result = _coerce_result({"key": "val"}, str)
        assert isinstance(result, str)

    def test_non_scalar_annotation_passthrough(self):
        """list[str] annotation should not trigger coercion."""
        result = _coerce_result(["a", "b"], list[str])
        assert result == ["a", "b"]

    def test_none_target_type_passthrough(self):
        """None annotation passes through unchanged."""
        result = _coerce_result({"x": 1}, None)
        assert result == {"x": 1}

    def test_dict_result_optional_str_is_json_string(self):
        """dict result for str | None must be valid JSON string (not repr)."""
        import json

        result = _coerce_result({"a": 1}, str | None)
        parsed = json.loads(result)
        assert parsed == {"a": 1}

    # --- None preservation (Codex #1281 regression) ---

    def test_none_result_optional_str_preserved(self):
        """A legitimately-None result for `str | None` must stay None, not become
        the literal string 'None'."""
        assert _coerce_result(None, str | None) is None

    def test_none_result_optional_int_preserved(self):
        """None for `int | None` must stay None, not raise from int(None)."""
        assert _coerce_result(None, int | None) is None

    def test_none_result_optional_float_preserved(self):
        assert _coerce_result(None, float | None) is None

    def test_none_result_required_scalar_passes_through(self):
        """None for a required scalar passes through so model_validate raises a
        clear validation error instead of silently coercing to 'None'."""
        assert _coerce_result(None, str) is None

    def test_none_revalidates_optional_field_to_none(self):
        """End-to-end: an action returning None for an Optional field stays None
        through revalidate_with_action_results (was corrupted to 'None')."""
        ac = ActionCall(name="opt", function="maybe", arguments={}, raw_call="maybe()")
        m = OptionalModel.model_construct(note=ac)
        result = revalidate_with_action_results(m, {"opt": None})
        assert result.note is None


# ── New edge cases ─────────────────────────────────────────────────────────────


class TestLNDLOutputLargeFields:
    def test_large_fields_dict_accessible(self):
        fields = {f"key_{i}": float(i) for i in range(1000)}
        out = LNDLOutput(fields=fields, lvars={}, lacts={}, actions={}, raw_out_block="")
        assert out["key_0"] == 0.0
        assert out["key_999"] == 999.0

    def test_large_fields_dict_getattr(self):
        fields = {f"f{i}": i for i in range(500)}
        out = LNDLOutput(fields=fields, lvars={}, lacts={}, actions={}, raw_out_block="")
        assert out.f0 == 0
        assert out.f499 == 499


class TestRevalidateWithNestedActionCalls:
    def test_nested_basemodel_action_call_at_depth_2(self):
        # NestedModel.inner.title holds an ActionCall — depth > 1
        inner_ac = ActionCall(name="inner_act", function="fetch", arguments={}, raw_call="fetch()")
        inner = SimpleModel.model_construct(title=inner_ac, score=0.0)
        outer = NestedModel.model_construct(name="outer", inner=inner)
        result = revalidate_with_action_results(outer, {"inner_act": "fetched-title"})
        assert result.inner.title == "fetched-title"
        assert result.name == "outer"


class TestCoerceResultComplexUnions:
    def test_str_int_none_union_passes_through(self):
        # str | int | None is not an Optional scalar (3 members including None)
        # _unwrap_scalar returns None → passthrough
        from typing import Union

        result = _coerce_result("hello", Union[str, int, None])
        assert result == "hello"

    def test_int_float_union_passes_through(self):
        from typing import Union

        result = _coerce_result(42, Union[int, float])
        assert result == 42

    def test_list_str_annotation_passes_through(self):
        result = _coerce_result(["a", "b", "c"], list[str])
        assert result == ["a", "b", "c"]


class TestActionCallEdgeCases:
    def test_empty_arguments_dict_accepted(self):
        ac = ActionCall(name="act", function="fn", arguments={}, raw_call="fn()")
        assert ac.arguments == {}

    def test_function_with_non_empty_name_accepted(self):
        ac = ActionCall(name="x", function="my_func", arguments={"a": 1}, raw_call="my_func(a=1)")
        assert ac.function == "my_func"

    def test_frozen_prevents_mutation(self):
        ac = ActionCall(name="a", function="f", arguments={}, raw_call="f()")
        with pytest.raises((AttributeError, TypeError)):
            ac.name = "b"
