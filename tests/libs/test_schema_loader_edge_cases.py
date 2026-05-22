# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Additional coverage for load_pydantic_model_from_schema."""

import pytest
from pydantic import ValidationError

from lionagi.libs.schema import load_pydantic_model_from_schema as lpms
from lionagi.libs.schema.load_pydantic_model_from_schema import (
    _CreateModelUnsupportedError,
    _make_enum,
    _resolve_ref,
    _sanitize_model_name,
    load_pydantic_model_from_schema,
)


class TestSanitizeAndEnum:
    def test_sanitize_valid_name(self):
        assert _sanitize_model_name("My Model") == "MyModel"

    def test_sanitize_starts_with_digit_returns_none(self):
        assert _sanitize_model_name("1bad") is None

    def test_sanitize_empty_returns_none(self):
        assert _sanitize_model_name("") is None

    def test_make_enum_dedupes_collisions(self):
        # Two values that collapse to same member name trigger the uniqueness loop.
        e = _make_enum("E", ["foo bar", "foo-bar"])
        assert len(list(e)) == 2


class TestResolveRefErrors:
    def test_non_local_ref_raises(self):
        with pytest.raises(_CreateModelUnsupportedError, match="Non-local"):
            _resolve_ref("http://x/y", {})

    def test_ref_target_missing(self):
        with pytest.raises(_CreateModelUnsupportedError, match="not found"):
            _resolve_ref("#/$defs/Missing", {"$defs": {}})

    def test_ref_resolves_to_non_dict(self):
        # $ref target exists but is a list, not a dict — hits line 118.
        root = {"a": [1, 2, 3]}
        with pytest.raises(_CreateModelUnsupportedError, match="non-dict"):
            _resolve_ref("#/a", root)

    def test_ref_path_traverses_non_dict(self):
        # Mid-traversal through a non-dict node — hits line 115.
        root = {"a": [1, 2, 3]}
        with pytest.raises(_CreateModelUnsupportedError, match="Cannot resolve"):
            _resolve_ref("#/a/0", root)


class TestSchemaInputHandling:
    def test_invalid_json_string_raises_valueerror(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_pydantic_model_from_schema("{not json")

    def test_wrong_type_raises_typeerror(self):
        with pytest.raises(TypeError):
            load_pydantic_model_from_schema(12345)  # type: ignore[arg-type]

    def test_json_string_accepted(self):
        schema = '{"type": "object", "properties": {"n": {"type": "integer"}}}'
        cls = load_pydantic_model_from_schema(schema, "M")
        inst = cls(n=3)
        assert inst.n == 3

    def test_title_resolves_model_name(self):
        schema = {
            "title": "MyTitled",
            "type": "object",
            "properties": {"x": {"type": "string"}},
        }
        cls = load_pydantic_model_from_schema(schema, "Fallback")
        assert cls.__name__ == "MyTitled"


class TestPrimitiveAndNested:
    def test_missing_type_without_properties_is_any(self):
        schema = {
            "type": "object",
            "properties": {"anything": {}},
        }
        cls = load_pydantic_model_from_schema(schema, "M")
        inst = cls(anything={"a": 1})
        assert inst.anything == {"a": 1}

    def test_missing_type_with_properties_treated_as_object(self):
        schema = {
            "type": "object",
            "properties": {
                "nested": {
                    "properties": {"x": {"type": "integer"}},
                }
            },
        }
        cls = load_pydantic_model_from_schema(schema, "M")
        inst = cls(nested={"x": 5})
        assert inst.nested.x == 5

    def test_type_list_with_object_nested(self):
        schema = {
            "type": "object",
            "properties": {
                "f": {
                    "type": ["object", "null"],
                    "properties": {"k": {"type": "string"}},
                }
            },
        }
        cls = load_pydantic_model_from_schema(schema, "M")
        assert cls(f={"k": "v"}).f.k == "v"
        assert cls(f=None).f is None

    def test_type_list_with_array(self):
        schema = {
            "type": "object",
            "properties": {"ls": {"type": ["array", "null"], "items": {"type": "integer"}}},
        }
        cls = load_pydantic_model_from_schema(schema, "M")
        assert cls(ls=[1, 2]).ls == [1, 2]

    def test_type_list_with_plain_object_no_properties(self):
        schema = {
            "type": "object",
            "properties": {"m": {"type": ["object", "null"]}},
        }
        cls = load_pydantic_model_from_schema(schema, "M")
        assert cls(m={"x": 1}).m == {"x": 1}

    def test_type_list_single_type(self):
        schema = {
            "type": "object",
            "properties": {"s": {"type": ["string"]}},
        }
        cls = load_pydantic_model_from_schema(schema, "M")
        assert cls(s="ok").s == "ok"

    def test_type_list_unsupported_falls_back_to_codegen(self):
        # A genuinely unknown JSON-schema type forces _CreateModelUnsupportedError.
        # Without datamodel_code_generator wired up, the loader will raise RuntimeError.
        schema = {
            "type": "object",
            "properties": {"x": {"type": ["frobnicate"]}},
        }
        # Either RuntimeError (no fallback) or ValueError bubbles up — we just
        # want to exercise the unsupported path.
        with pytest.raises((RuntimeError, _CreateModelUnsupportedError)):
            # Force primary path only by monkey-stubbing fallback presence
            old = lpms._HAS_DATAMODEL_CODE_GENERATOR
            lpms._HAS_DATAMODEL_CODE_GENERATOR = False
            try:
                load_pydantic_model_from_schema(schema, "M")
            finally:
                lpms._HAS_DATAMODEL_CODE_GENERATOR = old


class TestAnyOfOneOfAllOf:
    def test_anyof_single_variant(self):
        schema = {
            "type": "object",
            "properties": {"x": {"anyOf": [{"type": "string"}]}},
        }
        cls = load_pydantic_model_from_schema(schema, "M")
        assert cls(x="ok").x == "ok"

    def test_oneof_multiple_variants(self):
        schema = {
            "type": "object",
            "properties": {"x": {"oneOf": [{"type": "string"}, {"type": "integer"}]}},
        }
        cls = load_pydantic_model_from_schema(schema, "M")
        assert cls(x="s").x == "s"
        assert cls(x=3).x == 3

    def test_allof_with_ref(self):
        schema = {
            "type": "object",
            "$defs": {
                "Partial": {
                    "type": "object",
                    "properties": {"k": {"type": "string"}},
                }
            },
            "properties": {
                "v": {"allOf": [{"$ref": "#/$defs/Partial"}]},
            },
        }
        cls = load_pydantic_model_from_schema(schema, "M")
        inst = cls(v={"k": "hi"})
        assert inst.v.k == "hi"


class TestRequiredAndDefault:
    def test_required_with_default(self):
        schema = {
            "type": "object",
            "required": ["n"],
            "properties": {"n": {"type": "integer", "default": 7, "description": "num"}},
        }
        cls = load_pydantic_model_from_schema(schema, "M")
        inst = cls()
        assert inst.n == 7

    def test_required_without_default_with_description(self):
        schema = {
            "type": "object",
            "required": ["n"],
            "properties": {"n": {"type": "integer", "description": "must"}},
        }
        cls = load_pydantic_model_from_schema(schema, "M")
        with pytest.raises(ValidationError):
            cls()  # missing required
        assert cls(n=1).n == 1


class TestUnsupportedType:
    def test_unknown_scalar_type_unsupported(self):
        # "widget" is not in _JSON_TYPE_MAP and isn't object/array.
        schema = {
            "type": "object",
            "properties": {"w": {"type": "widget"}},
        }
        # Disable fallback so the unsupported error surfaces directly as RuntimeError.
        old = lpms._HAS_DATAMODEL_CODE_GENERATOR
        lpms._HAS_DATAMODEL_CODE_GENERATOR = False
        try:
            with pytest.raises(RuntimeError):
                load_pydantic_model_from_schema(schema, "M")
        finally:
            lpms._HAS_DATAMODEL_CODE_GENERATOR = old


class TestCodegenFallback:
    def test_schema_loader_raises_runtime_error_when_codegen_fallback_unavailable(
        self, monkeypatch
    ):
        """Monkeypatching _create_model_from_schema to raise unsupported, with
        _HAS_DATAMODEL_CODE_GENERATOR=False, must produce RuntimeError."""
        import lionagi.libs.schema.load_pydantic_model_from_schema as lpms

        def _always_unsupported(schema_dict, model_name):
            raise lpms._CreateModelUnsupportedError("unsupported")

        monkeypatch.setattr(lpms, "_create_model_from_schema", _always_unsupported)
        monkeypatch.setattr(lpms, "_HAS_DATAMODEL_CODE_GENERATOR", False)

        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        with pytest.raises(RuntimeError, match="datamodel-code-generator"):
            lpms.load_pydantic_model_from_schema(schema, "M")
