# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.operations.schema (relocated from protocols.structure)."""

import pytest
from pydantic import BaseModel

from lionagi.operations.schema.json_structure import JsonStructure
from lionagi.operations.schema.structure import Structure

# ---------------------------------------------------------------------------
# Structure tests
# ---------------------------------------------------------------------------


def test_structure_import_from_operations_schema():
    """Structure is importable from new canonical location."""
    assert Structure is not None


def test_json_structure_import_from_operations_schema():
    """JsonStructure is importable from new canonical location."""
    assert JsonStructure is not None


def test_structure_package_init():
    """Both classes importable from operations.schema package."""
    from lionagi.operations.schema import JsonStructure as JS
    from lionagi.operations.schema import Structure as S

    assert S is Structure
    assert JS is JsonStructure


def test_structure_init_no_base():
    s = Structure()
    assert s.name == "Structure"
    assert s.base is None
    assert s.base_dict is None
    assert not s.is_dict_mode


def test_structure_init_with_base_model():
    class Foo(BaseModel):
        x: int

    s = Structure(Foo)
    assert s.base is Foo
    assert s.name == "Foo"
    assert not s.is_dict_mode


def test_structure_init_with_dict():
    d = {"key": "string", "count": "integer"}
    s = Structure(d)
    assert s.is_dict_mode
    assert s.base_dict == d


def test_structure_with_actions():
    s = Structure().with_actions()
    assert s._actions is True


def test_structure_with_reason():
    s = Structure().with_reason()
    assert s._reason is True


def test_structure_repr():
    s = Structure()
    r = repr(s)
    assert "Structure" in r


# ---------------------------------------------------------------------------
# JsonStructure tests
# ---------------------------------------------------------------------------


def test_json_structure_format_response_format_basic():
    response_format = {"name": "string", "age": "integer"}
    result = JsonStructure._format_response_format(response_format)
    assert "MUST RETURN JSON-PARSEABLE RESPONSE" in result
    assert "```json" in result
    assert "name" in result
    assert "age" in result


def test_json_structure_format_response_format_none():
    assert JsonStructure._format_response_format(None) == ""


def test_json_structure_format_response_format_empty():
    assert JsonStructure._format_response_format({}) == ""


def test_json_structure_render_dict_mode():
    d = {"field": "string"}
    js = JsonStructure(d)
    rendered = js.render()
    assert "field" in rendered


def test_json_structure_parse_dict_mode():
    d = {"name": "string"}
    js = JsonStructure(d)
    result = js.parse('{"name": "Alice"}')
    assert result == {"name": "Alice"}


# ---------------------------------------------------------------------------
# Backward-compat shim tests (old import paths still work)
# ---------------------------------------------------------------------------


def test_deprecated_shim_base():
    """protocols.structure.base still exports Structure."""
    from lionagi.protocols.structure.base import Structure as OldStructure

    assert OldStructure is Structure


def test_deprecated_shim_json_structure():
    """protocols.structure.json_structure still exports JsonStructure."""
    from lionagi.protocols.structure.json_structure import JsonStructure as OldJS

    assert OldJS is JsonStructure


def test_deprecated_shim_package():
    """protocols.structure package still exports both names."""
    import lionagi.protocols.structure as old_pkg

    assert old_pkg.Structure is Structure
    assert old_pkg.JsonStructure is JsonStructure
