# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Characterize redacted error text separately from structured error details."""

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from lionhub.providers.anthropic import claude_code as claude
from lionhub.providers.google import gemini_code as gemini
from lionhub.providers.openai import codex
from lionhub.providers.pi import cli as pi


class _HideInputProbe(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    path: str

    @field_validator("path")
    @classmethod
    def reject(cls, _value):
        raise ValueError("path is unsafe")


def _observed(error, marker):
    return {
        "str": marker in str(error),
        "repr": marker in repr(error),
        "errors_default": marker in repr(error.errors()),
        "json_default": marker in error.json(),
        "errors_without_input": marker in repr(error.errors(include_input=False)),
        "json_without_input": marker in error.json(include_input=False),
    }


def test_installed_pydantic_can_hide_input_in_text_and_explicit_details():
    marker = "sensitive fixture content"
    try:
        _HideInputProbe(path=marker)
    except ValidationError as error:
        observed = _observed(error, marker)
    else:
        pytest.fail("probe unexpectedly accepted an invalid field", pytrace=False)
    print(f"pydantic support: {observed}")
    assert not observed["str"] and not observed["repr"]
    assert not observed["errors_without_input"] and not observed["json_without_input"]


@pytest.mark.parametrize(
    ("module", "request_type", "field", "batch"),
    [
        (claude, claude.ClaudeCodeRequest, "system_prompt_file", False),
        (codex, codex.CodexCodeRequest, "output_schema", False),
        (gemini, gemini.GeminiCodeRequest, "include_directories", True),
        (pi, pi.PiCodeRequest, "extension", True),
    ],
    ids=["claude", "codex", "gemini", "pi"],
)
def test_request_validation_error_text_hides_original_path(
    monkeypatch, tmp_path, module, request_type, field, batch
):
    # Isolate the provider's ValidationError carrier from the separate helper fix.
    def safe_refusal(_value, _name, **_kwargs):
        raise ValueError("path contains traversal")

    monkeypatch.setattr(module, "check_paths_safe" if batch else "check_path_safe", safe_refusal)
    marker = "sensitive fixture content"
    value = "../" + marker
    try:
        request_type(prompt="hello", repo=tmp_path, **{field: [value] if batch else value})
    except ValidationError as error:
        observed = _observed(error, marker)
    else:
        pytest.fail("invalid path was accepted", pytrace=False)
    print(f"provider {request_type.__name__}: {observed}")
    # The default structured APIs remain caller-controlled and are characterized,
    # not asserted to be private by this test.
    if observed["str"] or observed["repr"]:
        pytest.fail("validation error text retains the input", pytrace=False)


@pytest.mark.parametrize(
    ("request_type", "field", "batch"),
    [
        (claude.ClaudeCodeRequest, "system_prompt_file", False),
        (codex.CodexCodeRequest, "output_schema", False),
        (gemini.GeminiCodeRequest, "include_directories", True),
        (pi.PiCodeRequest, "extension", True),
    ],
    ids=["claude", "codex", "gemini", "pi"],
)
@pytest.mark.parametrize("kind", ["nul", "traversal", "absolute", "windows"])
def test_real_request_path_refusal_does_not_show_the_input(
    tmp_path, request_type, field, batch, kind
):
    marker = "sensitive fixture content"
    value = {
        "nul": marker + "\x00file",
        "traversal": "../" + marker,
        "absolute": "/" + marker,
        "windows": "C:/" + marker,
    }[kind]
    error = None
    try:
        request_type(prompt="hello", repo=tmp_path, **{field: [value] if batch else value})
    except Exception as caught:
        error = caught
    if error is None:
        pytest.fail("invalid path was accepted", pytrace=False)
    leaked = marker in str(error) or marker in repr(error)
    if leaked:
        pytest.fail("path refusal text retains the input", pytrace=False)
