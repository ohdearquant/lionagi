# Copyright (c) 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Generated schema modules use a digest of the generator's actual input."""

from __future__ import annotations

import hashlib
import importlib
from types import SimpleNamespace

import pytest

from lionagi.ln import MAX_HASH_INPUT_BYTES

schema_loader = importlib.import_module("lionagi.libs.schema.load_pydantic_model_from_schema")


@pytest.fixture
def generated_modules(monkeypatch):
    generated = []
    schema_kind = object()

    def write_model(source, *, output, **kwargs):
        assert kwargs["input_file_type"] is schema_kind
        generated.append((source, output.name))
        output.write_text(
            "from pydantic import BaseModel\n\nclass MiXeDModel(BaseModel):\n    value: int\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(schema_loader, "generate", write_model)
    monkeypatch.setattr(schema_loader, "InputFileType", SimpleNamespace(JsonSchema=schema_kind))
    return generated


def _expected_stem(source):
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return f"mixedmodel_model_{digest}"


@pytest.mark.parametrize(
    "source",
    [
        '{"type":"object","description":"plain text"}',
        '{"type":"object","description":"café αβ"}',
    ],
    ids=["ascii", "unicode"],
)
def test_generated_filename_and_class_module_use_utf8_digest(source, generated_modules):
    model = schema_loader._load_via_codegen({}, source, "MiXeDModel", None, None)

    expected = _expected_stem(source)
    assert generated_modules[0][1] == f"{expected}.py"
    assert model.__module__ == expected
    assert model.__name__ == "MiXeDModel"
    assert model(value="7").value == 7


def test_repeated_and_distinct_inputs_get_their_own_stems(generated_modules):
    first = '{"type":"object","description":"first"}'
    second = '{"type":"object","description":"second"}'
    sources = [first, first, second]
    models = [
        schema_loader._load_via_codegen({}, source, "MiXeDModel", None, None) for source in sources
    ]

    expected = [_expected_stem(source) for source in sources]
    assert expected[0] == expected[1]
    assert expected[0] != expected[2]
    assert [name for _, name in generated_modules] == [f"{stem}.py" for stem in expected]
    assert [model.__module__ for model in models] == expected


def test_public_opt_in_names_module_from_the_serialized_generator_input(
    monkeypatch, generated_modules
):
    def unsupported(*args):
        raise schema_loader._CreateModelUnsupportedError("controlled unsupported schema")

    monkeypatch.setattr(schema_loader, "_create_model_from_schema", unsupported)
    monkeypatch.setattr(schema_loader, "_HAS_DATAMODEL_CODE_GENERATOR", True)
    monkeypatch.setattr(schema_loader, "DataModelType", None)
    schema = {"title": "MiXeDModel", "type": "object", "description": "café"}

    model = schema_loader.load_pydantic_model_from_schema(schema, allow_codegen=True)

    assert len(generated_modules) == 1
    source, filename = generated_modules[0]
    expected = _expected_stem(source)
    assert filename == f"{expected}.py"
    assert model.__module__ == expected
    assert model(value="8").value == 8


def test_materialized_input_above_default_hash_cap_still_generates(generated_modules):
    source = '{"type":"object","description":"' + "x" * MAX_HASH_INPUT_BYTES + '"}'
    assert len(source.encode("utf-8")) > MAX_HASH_INPUT_BYTES

    model = schema_loader._load_via_codegen({}, source, "MiXeDModel", None, None)

    expected = _expected_stem(source)
    assert generated_modules[0][1] == f"{expected}.py"
    assert model.__module__ == expected
