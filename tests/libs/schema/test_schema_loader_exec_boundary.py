# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven tests for load_pydantic_model_from_schema exec boundary.

The schema loader previously fell back to datamodel-code-generator + exec_module()
when create_model() could not handle a schema, regardless of whether the schema
came from a trusted source. Caller-controlled schema data could reach dynamic
Python module execution.

Fix: The codegen/exec fallback requires explicit allow_codegen=True.
Default is allow_codegen=False — unsupported schemas raise RuntimeError
before any code generation or exec_module call.

These tests verify:
1. allow_codegen=False (default) blocks the exec path.
2. _load_via_codegen is never called without allow_codegen=True.
3. Simple schemas that create_model() handles still work normally.
"""

import unittest.mock

import pytest
from pydantic import BaseModel

from lionagi.libs.schema.load_pydantic_model_from_schema import (
    _CreateModelUnsupportedError,
    load_pydantic_model_from_schema,
)

# ---------------------------------------------------------------------------
# Security boundary: exec path is blocked by default
# ---------------------------------------------------------------------------


class TestExecBoundaryDefault:
    """allow_codegen defaults to False — exec path must be unreachable."""

    def test_simple_schema_succeeds_without_codegen(self):
        """create_model() handles simple object schemas; no codegen needed."""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        }
        model_cls = load_pydantic_model_from_schema(schema)
        assert issubclass(model_cls, BaseModel)
        instance = model_cls(name="Alice", age=30)
        assert instance.name == "Alice"

    def test_string_schema_succeeds(self):
        """JSON string input also works via create_model() path."""
        schema_str = '{"type": "object", "properties": {"x": {"type": "number"}}}'
        model_cls = load_pydantic_model_from_schema(schema_str)
        assert issubclass(model_cls, BaseModel)

    def test_unsupported_schema_raises_without_executing_code(self, monkeypatch):
        """When create_model fails and allow_codegen=False, RuntimeError is raised
        BEFORE _load_via_codegen is ever called — no code generation or exec."""
        import lionagi.libs.schema.load_pydantic_model_from_schema as loader_mod

        exec_called = []

        def fake_codegen(*a, **kw):
            exec_called.append(1)
            raise AssertionError("_load_via_codegen must not be called when allow_codegen=False")

        monkeypatch.setattr(loader_mod, "_load_via_codegen", fake_codegen)

        # Force create_model path to fail
        def fake_create_model(*a, **kw):
            raise _CreateModelUnsupportedError("forced failure for test")

        monkeypatch.setattr(loader_mod, "_create_model_from_schema", fake_create_model)

        with pytest.raises(RuntimeError, match="allow_codegen"):
            load_pydantic_model_from_schema({"type": "object"}, allow_codegen=False)

        assert not exec_called, "_load_via_codegen was called despite allow_codegen=False"

    def test_default_allow_codegen_is_false(self, monkeypatch):
        """Verify the default value of allow_codegen is False (regression guard)."""
        import inspect

        import lionagi.libs.schema.load_pydantic_model_from_schema as loader_mod

        sig = inspect.signature(loader_mod.load_pydantic_model_from_schema)
        param = sig.parameters.get("allow_codegen")
        assert param is not None, "allow_codegen parameter missing from function signature"
        assert param.default is False, (
            f"allow_codegen default changed to {param.default!r}; must remain False"
        )

    def test_allow_codegen_false_error_message_guides_caller(self, monkeypatch):
        """Error message must mention allow_codegen so callers can find the opt-in."""
        import lionagi.libs.schema.load_pydantic_model_from_schema as loader_mod

        def fake_create_model(*a, **kw):
            raise _CreateModelUnsupportedError("unsupported")

        monkeypatch.setattr(loader_mod, "_create_model_from_schema", fake_create_model)

        with pytest.raises(RuntimeError) as exc_info:
            load_pydantic_model_from_schema({"type": "object"}, allow_codegen=False)

        assert "allow_codegen" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Normal operation: create_model path works for common schemas
# ---------------------------------------------------------------------------


class TestNormalCreateModelPath:
    def test_nested_object_schema(self):
        schema = {
            "type": "object",
            "properties": {
                "address": {
                    "type": "object",
                    "properties": {"street": {"type": "string"}},
                }
            },
        }
        model_cls = load_pydantic_model_from_schema(schema)
        assert issubclass(model_cls, BaseModel)

    def test_array_type_schema(self):
        schema = {
            "type": "object",
            "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
        }
        model_cls = load_pydantic_model_from_schema(schema)
        assert issubclass(model_cls, BaseModel)

    def test_invalid_json_string_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_pydantic_model_from_schema("{not valid json}")

    def test_non_string_non_dict_raises_type_error(self):
        with pytest.raises(TypeError):
            load_pydantic_model_from_schema(12345)  # type: ignore[arg-type]
