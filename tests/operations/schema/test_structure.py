# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import pytest
from pydantic import BaseModel

from lionagi.operations.schema.json_structure import JsonStructure
from lionagi.operations.schema.structure import Structure


def test_structure_import_from_operations_schema():
    assert Structure is not None


def test_json_structure_import_from_operations_schema():
    assert JsonStructure is not None


def test_structure_package_init():
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


def test_deprecated_shim_base():
    from lionagi.protocols.structure.base import Structure as OldStructure

    assert OldStructure is Structure


def test_deprecated_shim_json_structure():
    from lionagi.protocols.structure.json_structure import JsonStructure as OldJS

    assert OldJS is JsonStructure


def test_deprecated_shim_package():
    import lionagi.protocols.structure as old_pkg

    assert old_pkg.Structure is Structure
    assert old_pkg.JsonStructure is JsonStructure


def test_deprecated_shim_base_full_surface():
    from typing import Any as TypingAny

    from pydantic import BaseModel as PydanticBaseModel

    from lionagi.ln.types import Operable as LnOperable
    from lionagi.ln.types import Spec as LnSpec
    from lionagi.operations.schema.structure import Structure as NewStructure
    from lionagi.protocols.structure.base import Any as ShimAny
    from lionagi.protocols.structure.base import BaseModel as ShimBaseModel
    from lionagi.protocols.structure.base import Operable as ShimOperable
    from lionagi.protocols.structure.base import Spec as ShimSpec
    from lionagi.protocols.structure.base import Structure as ShimStructure

    assert ShimStructure is NewStructure
    assert ShimOperable is LnOperable
    assert ShimSpec is LnSpec
    assert ShimBaseModel is PydanticBaseModel
    assert ShimAny is TypingAny


def test_deprecated_shim_json_structure_full_surface():
    import logging

    import orjson as real_orjson

    from lionagi.ln.fuzzy import FuzzyMatchKeysParams as LnFuzzyMatchKeysParams
    from lionagi.operations.schema.json_structure import _DEFAULT_FUZZY as NewDefaultFuzzy
    from lionagi.operations.schema.json_structure import (
        JsonStructure as NewJsonStructure,
    )
    from lionagi.operations.schema.structure import Structure as NewStructure
    from lionagi.protocols.structure.json_structure import (
        _DEFAULT_FUZZY as ShimDefaultFuzzy,
    )
    from lionagi.protocols.structure.json_structure import (
        FuzzyMatchKeysParams as ShimFuzzyMatchKeysParams,
    )
    from lionagi.protocols.structure.json_structure import (
        JsonStructure as ShimJsonStructure,
    )
    from lionagi.protocols.structure.json_structure import (
        Structure as ShimStructure,
    )
    from lionagi.protocols.structure.json_structure import extract_json as ShimExtractJson
    from lionagi.protocols.structure.json_structure import (
        fuzzy_validate_mapping as ShimFuzzyValidate,
    )
    from lionagi.protocols.structure.json_structure import logger as ShimLogger
    from lionagi.protocols.structure.json_structure import logging as ShimLogging
    from lionagi.protocols.structure.json_structure import orjson as ShimOrjson

    assert ShimJsonStructure is NewJsonStructure
    assert ShimStructure is NewStructure
    assert ShimFuzzyMatchKeysParams is LnFuzzyMatchKeysParams
    assert ShimDefaultFuzzy is NewDefaultFuzzy
    assert isinstance(ShimLogger, logging.Logger)
    assert ShimLogging is logging
    assert ShimOrjson is real_orjson
    # callable identity for the function re-exports
    assert callable(ShimExtractJson)
    assert callable(ShimFuzzyValidate)


def test_deprecated_shim_all_matches_old_modules():
    from lionagi.protocols.structure import base as shim_base
    from lionagi.protocols.structure import json_structure as shim_json

    assert shim_base.__all__ == ("Structure",)
    assert shim_json.__all__ == ("JsonStructure",)


def test_deprecated_shim_annotations_binding_preserved():
    from lionagi.protocols.structure.base import annotations as base_ann
    from lionagi.protocols.structure.json_structure import (
        annotations as json_ann,
    )

    assert base_ann is json_ann  # both are the same __future__ feature object
