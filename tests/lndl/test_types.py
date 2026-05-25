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


def make_action_call(name="act1", fn="fetch", args=None):
    return ActionCall(
        name=name,
        function=fn,
        arguments=args or {"url": "http://x"},
        raw_call=f"{fn}(url='http://x')",
    )


class TestLvarMetadata:
    def test_basic_creation(self):
        m = LvarMetadata(model="Report", field="title", local_name="t", value="Hello")
        assert m.model == "Report"
        assert m.field == "title"
        assert m.local_name == "t"
        assert m.value == "Hello"

    def test_frozen(self):
        m = LvarMetadata(model="R", field="f", local_name="l", value="v")
        with pytest.raises((AttributeError, TypeError)):
            m.model = "X"


class TestRLvarMetadata:
    def test_basic_creation(self):
        m = RLvarMetadata(local_name="x", value="raw text")
        assert m.local_name == "x"
        assert m.value == "raw text"

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
    def test_basic(self):
        pc = ParsedConstructor(class_name="Report", kwargs={"title": "X"}, raw="Report(title='X')")
        assert pc.class_name == "Report"
        assert pc.kwargs == {"title": "X"}

    def test_has_dict_unpack_false(self):
        pc = ParsedConstructor(class_name="A", kwargs={"x": 1}, raw="A(x=1)")
        assert pc.has_dict_unpack is False

    def test_has_dict_unpack_true(self):
        pc = ParsedConstructor(class_name="A", kwargs={"**data": {}}, raw="A(**data)")
        assert pc.has_dict_unpack is True


class TestActionCall:
    def test_basic(self):
        ac = make_action_call()
        assert ac.name == "act1"
        assert ac.function == "fetch"
        assert ac.arguments == {"url": "http://x"}

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
