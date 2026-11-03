# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Path refusals do not retain input across request error surfaces."""

import pytest
from pydantic import ValidationError

from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest
from lionhub.providers.google.gemini_code import GeminiCodeRequest
from lionhub.providers.openai.codex import CodexCodeRequest
from lionhub.providers.pi.cli import PiCodeRequest

_CASES = [
    (ClaudeCodeRequest, "system_prompt_file", False),
    (CodexCodeRequest, "output_schema", False),
    (GeminiCodeRequest, "include_directories", True),
    (PiCodeRequest, "extension", True),
]


def _contains_marker(error, marker, *, structured):
    values = [str(error), repr(error)]
    if structured and callable(getattr(error, "errors", None)):
        values.append(repr(error.errors()))
    if structured and callable(getattr(error, "json", None)):
        values.append(error.json())
    return any(marker in value for value in values)


def _in_chain(error, marker):
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if _contains_marker(current, marker, structured=current is not error):
            return True
        pending.extend(x for x in (current.__cause__, current.__context__) if x is not None)
    return False


@pytest.mark.parametrize(
    "request_type,field,batch", _CASES, ids=["claude", "codex", "gemini", "pi"]
)
@pytest.mark.parametrize("kind", ["traversal", "symlink"])
@pytest.mark.parametrize("method", ["constructor", "model_validate"])
@pytest.mark.parametrize("surface", ["str", "repr", "chain"])
def test_request_path_error_surface_has_no_input(
    tmp_path, request_type, field, batch, kind, method, surface
):
    marker = "sensitive fixture content"
    repo = tmp_path / "repo"
    repo.mkdir()
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / "alias").symlink_to(outside, target_is_directory=True)
    value = "../" + marker if kind == "traversal" else "alias/" + marker
    payload = {"prompt": "hello", "repo": repo, field: [value] if batch else value}
    error = None
    try:
        if method == "constructor":
            request_type(**payload)
        else:
            request_type.model_validate(payload)
    except Exception as caught:
        error = caught
    if error is None:
        pytest.fail("invalid path was accepted", pytrace=False)
    if not isinstance(error, ValidationError):
        pytest.fail("path error no longer has its validation type", pytrace=False)
    if surface == "str":
        leaked = marker in str(error)
    elif surface == "repr":
        leaked = marker in repr(error)
    else:
        leaked = _in_chain(error, marker)
    if leaked:
        pytest.fail("request error surface retains caller input", pytrace=False)


@pytest.mark.parametrize("request_type", [ClaudeCodeRequest, CodexCodeRequest])
@pytest.mark.parametrize("surface", ["str", "repr", "chain"])
def test_add_directory_traversal_is_not_exposed(tmp_path, request_type, surface):
    marker = "sensitive fixture content"
    error = None
    try:
        request_type(prompt="hello", repo=tmp_path, add_dir=["../" + marker])
    except Exception as caught:
        error = caught
    if not isinstance(error, ValidationError):
        pytest.fail("invalid add_dir was not refused as validation error", pytrace=False)
    if surface == "str":
        leaked = marker in str(error)
    elif surface == "repr":
        leaked = marker in repr(error)
    else:
        leaked = _in_chain(error, marker)
    if leaked:
        pytest.fail("add_dir refusal retains caller input", pytrace=False)
