# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Only the serializer's exact depth error triggers the arguments fallback."""

import importlib

import orjson
import pytest

from lionagi.protocols.action.function_calling import FunctionCalling, Tool


def sample_tool(value: str) -> str:
    """Return the given value."""
    return value


def _call():
    return FunctionCalling(func_tool=Tool(func_callable=sample_tool), arguments={"value": "ok"})


def _inject_first_serializer_error(monkeypatch, error):
    json_dump = importlib.import_module("lionagi.ln._json_dump")
    original = json_dump.orjson.dumps
    attempts = []

    def controlled_dumps(obj, *args, **kwargs):
        attempts.append(obj)
        if len(attempts) == 1:
            raise error
        return original(obj, *args, **kwargs)

    monkeypatch.setattr(json_dump.orjson, "dumps", controlled_dumps)
    return attempts


class _OtherTypeError(TypeError):
    pass


@pytest.mark.parametrize(
    "error",
    [
        TypeError("different serializer: Recursion limit reached while processing input"),
        TypeError("Recursion limit reached", "extra context"),
        _OtherTypeError("Recursion limit reached"),
        TypeError("unsupported serializer payload"),
    ],
    ids=["embedded-message", "extra-argument", "subclass", "unrelated"],
)
def test_other_type_errors_propagate(monkeypatch, error):
    call = _call()
    attempts = _inject_first_serializer_error(monkeypatch, error)

    with pytest.raises(TypeError) as caught:
        call.to_dict(mode="json")

    assert caught.value is error
    assert len(attempts) == 1
    assert attempts[0]["arguments"] == {"value": "ok"}


@pytest.mark.parametrize("error", [TypeError("Recursion limit reached"), RecursionError()])
def test_measured_depth_errors_take_existing_fallback(monkeypatch, error):
    call = _call()
    attempts = _inject_first_serializer_error(monkeypatch, error)

    result = call.to_dict(mode="json")

    assert len(attempts) == 2
    assert attempts[0]["arguments"] == {"value": "ok"}
    assert attempts[1]["arguments"] == {}
    assert result["arguments"] == "<arguments too deeply nested>"
    assert call.arguments == {"value": "ok"}


@pytest.mark.parametrize("shape", ["deep", "cycle"])
def test_installed_orjson_depth_error_has_the_expected_exact_signature(shape):
    if shape == "deep":
        value = "leaf"
        for _ in range(255):
            value = [value]
    else:
        value = []
        value.append(value)

    with pytest.raises(TypeError) as caught:
        orjson.dumps(value)

    assert type(caught.value) is TypeError
    assert caught.value.args == ("Recursion limit reached",)
