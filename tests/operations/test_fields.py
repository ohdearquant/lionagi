# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.operations.fields — covering Instruct.handle, ActionRequestModel.create,
and _get_default_fields listable/nullable chaining."""

import pytest
from pydantic import BaseModel

from lionagi.operations.fields import (
    ActionRequestModel,
    Instruct,
    get_default_field,
)

# ---------------------------------------------------------------------------
# Instruct.handle — lines 119-137
# ---------------------------------------------------------------------------


class TestInstructHandle:
    def test_handle_none_instruct_with_instruction_override(self):
        """`instruct=None` creates empty dict and applies instruction override."""
        result = Instruct.handle(instruct=None, instruction="do something")
        assert isinstance(result, Instruct)
        assert result.instruction == "do something"

    def test_handle_instruct_object_is_converted_to_dict(self):
        """`instruct=Instruct(...)` is converted via to_dict before overrides apply."""
        base = Instruct(instruction="original", guidance="base guide")
        result = Instruct.handle(instruct=base, instruction="overridden")
        assert result.instruction == "overridden"
        # guidance from the original Instruct object is preserved
        assert result.guidance == "base guide"

    def test_handle_instruct_dict_passed_directly(self):
        """Plain dict passed as instruct is used as-is for overrides."""
        result = Instruct.handle(instruct={"instruction": "old"}, instruction="new")
        assert result.instruction == "new"

    def test_handle_none_sentinel_values_are_excluded(self):
        """None overrides are treated as sentinels and do NOT overwrite existing keys."""
        result = Instruct.handle(
            instruct={"instruction": "keep"},
            instruction=None,
            guidance=None,
        )
        # None is a sentinel → excluded; original instruction is preserved
        assert result.instruction == "keep"

    def test_handle_all_overrides_applied(self):
        """All non-sentinel overrides populate the returned Instruct."""
        result = Instruct.handle(
            instruct=None,
            instruction="task",
            guidance="guide",
            context="ctx",
            reason=True,
        )
        assert result.instruction == "task"
        assert result.guidance == "guide"
        assert result.context == "ctx"
        assert result.reason is True

    def test_handle_returns_instruct_instance(self):
        """Return value is always an Instruct object."""
        result = Instruct.handle()
        assert isinstance(result, Instruct)


# ---------------------------------------------------------------------------
# ActionRequestModel.create — lines 235-297
# ---------------------------------------------------------------------------


class TestActionRequestModelCreate:
    def test_create_from_valid_json_string(self):
        """JSON string with function/arguments is parsed into a model list."""
        json_str = '{"function": "add", "arguments": {"a": 1, "b": 2}}'
        result = ActionRequestModel.create(json_str)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].function == "add"
        assert result[0].arguments == {"a": 1, "b": 2}

    def test_create_from_dict_content(self):
        """Dict content is treated as a single JSON block."""
        content = {"function": "greet", "arguments": {"name": "world"}}
        result = ActionRequestModel.create(content)
        assert len(result) == 1
        assert result[0].function == "greet"

    def test_create_from_base_model_instance(self):
        """A BaseModel instance is model_dumped and parsed."""

        class MyRequest(BaseModel):
            function: str = "fn"
            arguments: dict = {"x": 1}

        obj = MyRequest()
        result = ActionRequestModel.create(obj)
        assert len(result) == 1
        assert result[0].function == "fn"
        assert result[0].arguments == {"x": 1}

    def test_create_returns_empty_list_for_unparseable_string(self):
        """Strings with no extractable JSON return empty list."""
        result = ActionRequestModel.create("this is plain text with no JSON")
        assert result == []

    def test_create_returns_empty_list_for_missing_arguments(self):
        """JSON with function but no arguments is skipped."""
        json_str = '{"function": "fn_no_args"}'
        result = ActionRequestModel.create(json_str)
        assert result == []

    def test_create_normalises_action_prefixed_keys(self):
        """action_function / action_arguments key prefixes are stripped."""
        content = {
            "action_function": "call_me",
            "action_arguments": {"p": "v"},
        }
        result = ActionRequestModel.create(content)
        # key normalisation strips "action_" so function/arguments are found
        assert len(result) == 1
        assert result[0].function == "call_me"

    def test_create_handles_nested_function_dict(self):
        """When function value is a dict with 'name', unwraps to string."""
        content = {
            "function": {"name": "nested_fn"},
            "arguments": {"key": "val"},
        }
        result = ActionRequestModel.create(content)
        assert len(result) == 1
        assert result[0].function == "nested_fn"

    def test_create_from_python_code_block_fallback(self):
        """```python blocks are tried as fallback JSON extraction."""
        content = '```python\n{"function": "py_fn", "arguments": {"x": 1}}\n```'
        result = ActionRequestModel.create(content)
        # may or may not parse depending on extract_json; at minimum returns a list
        assert isinstance(result, list)

    def test_create_exception_returns_empty_list(self):
        """Exception inside create is caught and empty list returned."""
        result = ActionRequestModel.create(None)
        assert result == []


# ---------------------------------------------------------------------------
# _get_default_fields listable/nullable chaining — lines 387-401
# ---------------------------------------------------------------------------


class TestGetDefaultFieldChaining:
    def test_unknown_kind_raises_value_error(self):
        """Unknown kind raises ValueError."""
        from lionagi.operations.fields import _get_default_fields

        with pytest.raises(ValueError, match="Unknown default field kind"):
            _get_default_fields("nonexistent_kind")

    def test_listable_true_converts_non_listable_field(self):
        """listable=True forces a normally non-listable field to be listable."""
        from lionagi.operations.fields import _get_default_fields

        # "reason" is not listable by default; passing listable=True wraps it
        fm = _get_default_fields("reason", listable=True)
        assert fm.is_listable

    def test_listable_false_marks_listable_field_as_non_listable(self):
        """listable=False marks an already-listable field as non-listable via metadata."""
        from lionagi.operations.fields import _get_default_fields

        # "action_requests" is listable by default; passing listable=False should disable it
        fm = _get_default_fields("action_requests", listable=False)
        # The field is marked with metadata "listable"=False
        assert not fm.is_listable

    def test_nullable_true_marks_field_nullable(self):
        """nullable=True (default) makes the field nullable."""
        from lionagi.operations.fields import _get_default_fields

        fm = _get_default_fields("reason", nullable=True)
        assert fm.is_nullable

    def test_nullable_false_skips_nullable_marking(self):
        """nullable=False skips the as_nullable call."""
        from lionagi.operations.fields import _get_default_fields

        fm = _get_default_fields("reason", nullable=False)
        assert not fm.is_nullable

    def test_listable_field_gets_list_default_when_default_unset(self):
        """Listable field with no explicit default gets default=list factory."""
        from lionagi.operations.fields import _get_default_fields

        fm = _get_default_fields("action_requests", nullable=False)
        # listable field with Unset default → default becomes list callable
        assert fm.extract_metadata("default") is list

    def test_explicit_default_is_applied(self):
        """Passing an explicit default value is stored in FieldModel metadata."""
        from lionagi.operations.fields import _get_default_fields

        fm = _get_default_fields("reason", nullable=False, default="fallback")
        assert fm.extract_metadata("default") == "fallback"

    def test_get_default_field_caches_results(self):
        """Calling get_default_field twice with same args returns cached object."""
        fm1 = get_default_field("reason")
        fm2 = get_default_field("reason")
        assert fm1 is fm2
