# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Malformed inline provider configuration is refused without echoing it."""

import pytest
from pydantic import ValidationError

from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest


@pytest.mark.parametrize("field", ["mcp_config", "settings"])
@pytest.mark.parametrize("method", ["constructor", "model_validate"])
@pytest.mark.parametrize("surface", ["str", "repr", "chain"])
def test_malformed_inline_configuration_is_refused_without_its_input(
    tmp_path, field, method, surface
):
    marker = "sensitive fixture content"
    value = '  {"setting":"' + marker + '"'
    request = {"prompt": "hello", "repo": tmp_path, field: value}
    error = None
    try:
        if method == "constructor":
            ClaudeCodeRequest(**request)
        else:
            ClaudeCodeRequest.model_validate(request)
    except Exception as caught:
        error = caught
    if not isinstance(error, ValidationError):
        pytest.fail(
            "malformed inline configuration was not refused as validation error", pytrace=False
        )
    if surface == "str":
        leaked = marker in str(error)
    elif surface == "repr":
        leaked = marker in repr(error)
    else:
        pending = [error]
        seen = set()
        leaked = False
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            leaked = leaked or marker in str(current) or marker in repr(current)
            pending.extend(x for x in (current.__cause__, current.__context__) if x is not None)
    if leaked:
        pytest.fail("malformed configuration error retains caller input", pytrace=False)


@pytest.mark.parametrize("field", ["mcp_config", "settings"])
def test_valid_inline_json_is_not_treated_as_a_path(tmp_path, field):
    value = '{"setting": "inside/../file"}'
    request = ClaudeCodeRequest(prompt="hello", repo=tmp_path, **{field: value})
    assert getattr(request, field) == value
