# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Build Pydantic models from JSON Schema dicts/strings."""

from __future__ import annotations

import importlib.util
import json
import logging
import string
import tempfile
from enum import Enum as StdEnum
from pathlib import Path
from typing import Any, Optional, TypeVar, Union

from pydantic import BaseModel, Field, PydanticUserError, create_model

from lionagi.libs.nested import deep_merge as _deep_merge
from lionagi.utils import is_import_installed

logger = logging.getLogger(__name__)

_HAS_DATAMODEL_CODE_GENERATOR = is_import_installed("datamodel_code_generator")

try:
    from datamodel_code_generator import (  # type: ignore[import]
        DataModelType,
        InputFileType,
        PythonVersion,
        generate,
    )
except ImportError:
    DataModelType = None
    InputFileType = None
    PythonVersion = None
    generate = None

B = TypeVar("B", bound=BaseModel)

# ---------------------------------------------------------------------------
# JSON-Schema type -> Python type mapping
# ---------------------------------------------------------------------------

_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


class _CreateModelUnsupportedError(Exception):
    """Raised when create_model cannot represent the schema."""


# ---------------------------------------------------------------------------
# Internal helpers for create_model approach
# ---------------------------------------------------------------------------


def _sanitize_model_name(raw: str) -> str | None:
    valid_chars = string.ascii_letters + string.digits + "_"
    sanitized = "".join(c for c in raw.replace(" ", "") if c in valid_chars)
    if sanitized and sanitized[0].isalpha():
        return sanitized
    return None


def _resolve_model_name(schema_dict: dict[str, Any], default: str) -> str:
    title = schema_dict.get("title")
    if title and isinstance(title, str):
        name = _sanitize_model_name(title)
        if name:
            return name
    return default


def _make_enum(name: str, values: list[Any]) -> type:
    members: dict[str, Any] = {}
    for v in values:
        member_name = str(v).upper().replace(" ", "_").replace("-", "_")
        # Ensure uniqueness
        base = member_name
        idx = 1
        while member_name in members:
            member_name = f"{base}_{idx}"
            idx += 1
        members[member_name] = v
    return StdEnum(name, members)  # type: ignore[return-value]


def _resolve_ref(ref: str, root_schema: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise _CreateModelUnsupportedError(f"Non-local $ref not supported: {ref}")

    parts = ref.lstrip("#/").split("/")
    node: Any = root_schema
    for part in parts:
        if isinstance(node, dict):
            node = node.get(part)
        else:
            raise _CreateModelUnsupportedError(f"Cannot resolve $ref path: {ref}")
        if node is None:
            raise _CreateModelUnsupportedError(f"$ref target not found: {ref}")
    if not isinstance(node, dict):
        raise _CreateModelUnsupportedError(f"$ref resolved to non-dict: {ref}")
    return node


def _schema_to_type(
    prop_schema: dict[str, Any],
    prop_name: str,
    root_schema: dict[str, Any],
    models_cache: dict[str, type[BaseModel]],
    parent_name: str,
) -> type:
    if "$ref" in prop_schema:
        resolved = _resolve_ref(prop_schema["$ref"], root_schema)
        ref_name = prop_schema["$ref"].rsplit("/", 1)[-1]
        return _build_model_from_object(resolved, ref_name, root_schema, models_cache)

    if "enum" in prop_schema:
        enum_name = f"{parent_name}_{prop_name}_enum".replace(" ", "")
        return _make_enum(enum_name, prop_schema["enum"])

    if "const" in prop_schema:
        enum_name = f"{parent_name}_{prop_name}_const".replace(" ", "")
        return _make_enum(enum_name, [prop_schema["const"]])

    for combo_key in ("anyOf", "oneOf"):
        if combo_key in prop_schema:
            variants = prop_schema[combo_key]
            py_types: list[type] = []
            for variant in variants:
                py_types.append(
                    _schema_to_type(variant, prop_name, root_schema, models_cache, parent_name)
                )
            if len(py_types) == 1:
                return py_types[0]
            return Union[tuple(py_types)]  # type: ignore[return-value]

    if "allOf" in prop_schema:
        merged: dict[str, Any] = {}
        for sub in prop_schema["allOf"]:
            if "$ref" in sub:
                sub = _resolve_ref(sub["$ref"], root_schema)
            merged = _deep_merge(merged, sub)
        return _schema_to_type(merged, prop_name, root_schema, models_cache, parent_name)

    schema_type = prop_schema.get("type")

    if schema_type is None:
        if "properties" in prop_schema:
            schema_type = "object"
        else:
            return Any  # type: ignore[return-value]

    if isinstance(schema_type, list):
        py_types_list: list[type] = []
        for t in schema_type:
            mapped = _JSON_TYPE_MAP.get(t)
            if mapped is None:
                if t == "object":
                    if "properties" in prop_schema:
                        nested_name = prop_schema.get("title") or f"{parent_name}_{prop_name}"
                        py_types_list.append(
                            _build_model_from_object(
                                prop_schema,
                                nested_name,
                                root_schema,
                                models_cache,
                            )
                        )
                    else:
                        py_types_list.append(dict)
                elif t == "array":
                    py_types_list.append(
                        _array_type(
                            prop_schema,
                            prop_name,
                            root_schema,
                            models_cache,
                            parent_name,
                        )
                    )
                else:
                    raise _CreateModelUnsupportedError(f"Unsupported type in list: {t}")
            else:
                py_types_list.append(mapped)
        if len(py_types_list) == 1:
            return py_types_list[0]
        return Union[tuple(py_types_list)]  # type: ignore[return-value]

    mapped = _JSON_TYPE_MAP.get(schema_type)
    if mapped is not None:
        return mapped

    if schema_type == "array":
        return _array_type(prop_schema, prop_name, root_schema, models_cache, parent_name)

    if schema_type == "object":
        if "properties" in prop_schema:
            nested_name = prop_schema.get("title") or f"{parent_name}_{prop_name}"
            return _build_model_from_object(prop_schema, nested_name, root_schema, models_cache)
        additional = prop_schema.get("additionalProperties")
        if isinstance(additional, dict):
            val_type = _schema_to_type(
                additional, prop_name, root_schema, models_cache, parent_name
            )
            return dict[str, val_type]  # type: ignore[valid-type]
        return dict  # type: ignore[return-value]

    raise _CreateModelUnsupportedError(f"Unsupported JSON Schema type: {schema_type}")


def _array_type(
    prop_schema: dict[str, Any],
    prop_name: str,
    root_schema: dict[str, Any],
    models_cache: dict[str, type[BaseModel]],
    parent_name: str,
) -> type:
    items = prop_schema.get("items")
    if items is None:
        return list  # type: ignore[return-value]
    item_type = _schema_to_type(items, f"{prop_name}_item", root_schema, models_cache, parent_name)
    return list[item_type]  # type: ignore[valid-type]


def _build_model_from_object(
    schema_dict: dict[str, Any],
    model_name: str,
    root_schema: dict[str, Any],
    models_cache: dict[str, type[BaseModel]],
) -> type[BaseModel]:
    safe_name = _sanitize_model_name(model_name) or "DynamicModel"

    if safe_name in models_cache:
        return models_cache[safe_name]

    properties = schema_dict.get("properties", {})
    required_fields = set(schema_dict.get("required", []))

    field_definitions: dict[str, Any] = {}

    for field_name, field_schema in properties.items():
        try:
            py_type = _schema_to_type(
                field_schema, field_name, root_schema, models_cache, safe_name
            )
        except _CreateModelUnsupportedError:
            raise

        description = field_schema.get("description")
        default = field_schema.get("default")
        field_kwargs: dict[str, Any] = {}
        if description:
            field_kwargs["description"] = description

        if field_name in required_fields:
            if default is not None:
                field_kwargs["default"] = default
                field_definitions[field_name] = (py_type, Field(**field_kwargs))
            elif field_kwargs:
                field_definitions[field_name] = (
                    py_type,
                    Field(..., **field_kwargs),
                )
            else:
                field_definitions[field_name] = (py_type, ...)
        else:
            # Optional field
            if default is not None:
                field_kwargs["default"] = default
                field_definitions[field_name] = (
                    Optional[py_type],  # noqa: UP045  # type: ignore[valid-type]
                    Field(**field_kwargs),
                )
            else:
                field_definitions[field_name] = (
                    Optional[py_type],  # noqa: UP045  # type: ignore[valid-type]
                    Field(default=None, **field_kwargs),
                )

    model_cls = create_model(safe_name, **field_definitions)  # type: ignore[call-overload]
    models_cache[safe_name] = model_cls
    return model_cls


def _create_model_from_schema(
    schema_dict: dict[str, Any],
    model_name: str,
) -> type[BaseModel]:
    root_schema = dict(schema_dict)

    models_cache: dict[str, type[BaseModel]] = {}

    for defs_key in ("$defs", "definitions"):
        defs = root_schema.get(defs_key, {})
        if isinstance(defs, dict):
            for def_name, def_schema in defs.items():
                if isinstance(def_schema, dict):
                    try:
                        _build_model_from_object(def_schema, def_name, root_schema, models_cache)
                    except _CreateModelUnsupportedError:
                        raise

    return _build_model_from_object(schema_dict, model_name, root_schema, models_cache)


# ---------------------------------------------------------------------------
# Fallback: datamodel-code-generator + exec_module
# ---------------------------------------------------------------------------


def _load_via_codegen(
    schema_dict: dict[str, Any],
    schema_input_data: str,
    resolved_model_name: str,
    pydantic_version: Any,
    python_version: Any,
) -> type[BaseModel]:
    with tempfile.TemporaryDirectory() as temporary_directory_name:
        temporary_directory = Path(temporary_directory_name)
        output_file = (
            temporary_directory
            / f"{resolved_model_name.lower()}_model_{hash(schema_input_data)}.py"
        )
        module_name = output_file.stem

        try:
            generate(
                schema_input_data,
                input_file_type=InputFileType.JsonSchema,
                input_filename="schema.json",
                output=output_file,
                output_model_type=pydantic_version,
                target_python_version=python_version,
                base_class="pydantic.BaseModel",
            )
        except Exception as e:
            error_msg = "Failed to generate model code"
            raise RuntimeError(error_msg) from e

        if not output_file.exists():
            error_msg = f"Generated model file was not created: {output_file}"
            raise FileNotFoundError(error_msg)

        spec = importlib.util.spec_from_file_location(module_name, str(output_file))
        if spec is None or spec.loader is None:
            error_msg = f"Could not create module spec for {output_file}"
            raise ImportError(error_msg)

        generated_module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(generated_module)
        except Exception as e:
            error_msg = f"Failed to load generated module ({output_file})"
            raise RuntimeError(error_msg) from e

        def _validate(m: Any) -> None:
            if not isinstance(m, type) or not issubclass(m, BaseModel):
                raise TypeError(
                    f"Found attribute '{resolved_model_name}' is not a Pydantic BaseModel class."
                )

        model_class: type[BaseModel]
        try:
            model_class = getattr(generated_module, resolved_model_name)
            _validate(model_class)
        except AttributeError:
            try:
                model_class = generated_module.Model
                _validate(model_class)
            except AttributeError as e:
                available = [
                    a
                    for a in dir(generated_module)
                    if isinstance(getattr(generated_module, a, None), type)
                    and issubclass(getattr(generated_module, a, object), BaseModel)
                    and getattr(generated_module, a, None) is not BaseModel
                ]
                raise AttributeError(
                    f"Could not find model '{resolved_model_name}' or 'Model' "
                    f"in generated module. Found: {available}"
                ) from e

        try:
            model_class.model_rebuild(_types_namespace=generated_module.__dict__, force=True)
        except (PydanticUserError, NameError) as e:
            raise RuntimeError(f"Error during model_rebuild for {resolved_model_name}") from e
        except Exception as e:
            raise RuntimeError(
                f"Unexpected error during model_rebuild for {resolved_model_name}"
            ) from e

        return model_class


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_pydantic_model_from_schema(
    schema: str | dict[str, Any],
    model_name: str = "DynamicModel",
    /,
    pydantic_version: Any = None,
    python_version: Any = None,
    allow_codegen: bool = False,
) -> type[BaseModel]:
    """Build a Pydantic model class from a JSON schema string or dict."""
    schema_dict: dict[str, Any]

    if isinstance(schema, dict):
        schema_dict = schema
    elif isinstance(schema, str):
        try:
            schema_dict = json.loads(schema)
        except json.JSONDecodeError as e:
            raise ValueError("Invalid JSON schema string provided") from e
    else:
        raise TypeError("Schema must be a JSON string or a dictionary.")

    resolved_model_name = _resolve_model_name(schema_dict, model_name)

    create_model_error: _CreateModelUnsupportedError | None = None
    try:
        return _create_model_from_schema(schema_dict, resolved_model_name)
    except _CreateModelUnsupportedError as exc:
        create_model_error = exc
        logger.debug(
            "create_model could not handle schema (%s); codegen fallback=%s.",
            exc,
            allow_codegen,
        )

    # Codegen fallback executes generated Python; opt-in only for trusted input.
    if not allow_codegen:
        raise RuntimeError(
            f"create_model could not handle this schema ({create_model_error}). "
            "The datamodel-code-generator fallback is disabled for untrusted input. "
            "Pass allow_codegen=True only for schemas from fully trusted sources."
        )

    if not _HAS_DATAMODEL_CODE_GENERATOR:
        raise RuntimeError(
            f"create_model could not handle this schema ({create_model_error}), and "
            "`datamodel-code-generator` is not installed for fallback. "
            "Install it with: pip install datamodel-code-generator"
        )

    if DataModelType is not None:
        pydantic_version = pydantic_version or DataModelType.PydanticV2BaseModel
        python_version = python_version or PythonVersion.PY_312

    try:
        from lionagi import ln

        schema_input_data = ln.json_dumps(schema_dict)
    except Exception:
        schema_input_data = json.dumps(schema_dict)

    return _load_via_codegen(
        schema_dict,
        schema_input_data,
        resolved_model_name,
        pydantic_version,
        python_version,
    )
