# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Claude text inputs are strict UTF-8; filesystem paths retain their byte semantics."""

from __future__ import annotations

import os

import pytest

from lionhub.providers.anthropic import claude_code as cc

_SECRET = "sensitive fixture value"

# These fields become text arguments directly. Repeated values put the value
# under test second, so the entire list must be checked.
_TEXT_ARGUMENTS = [
    ("resume", False),
    ("session_id", False),
    ("name", False),
    ("model", False),
    ("fallback_model", False),
    ("system_prompt", False),
    ("append_system_prompt", False),
    ("allowed_tools", True),
    ("disallowed_tools", True),
    ("tools", False),
    ("permission_prompt_tool_name", False),
    ("agent", False),
    ("json_schema_output", False),
    ("setting_sources", False),
    ("betas", True),
    ("worktree", False),
    ("debug", False),
]


@pytest.fixture
def fake_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: "claude")

    async def fake_ndjson(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if False:
            yield {}

    monkeypatch.setattr(cc, "ndjson_from_cli", fake_ndjson)
    return calls


async def _start(request):
    async for _ in cc._ndjson_from_cli(request):
        pass


def _assert_value_free(error):
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert _SECRET not in str(current)
        assert _SECRET not in repr(current)
        assert not isinstance(current, UnicodeEncodeError)
        pending.extend(
            item for item in (current.__cause__, current.__context__) if item is not None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["\udcff", "\ud800"], ids=["surrogate-escape", "lone-high"])
@pytest.mark.parametrize("field,repeated", _TEXT_ARGUMENTS, ids=[x[0] for x in _TEXT_ARGUMENTS])
async def test_text_argument_refuses_surrogates_without_retaining_input(
    tmp_path, fake_transport, field, repeated, bad
):
    value = _SECRET + bad
    argument = ["safe", value] if repeated else value
    with pytest.raises(ValueError) as caught:
        request = cc.ClaudeCodeRequest(prompt="hello", repo=tmp_path, **{field: argument})
        await _start(request)
    assert "surrogate" in str(caught.value).lower()
    _assert_value_free(caught.value)
    assert fake_transport == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["\udcff", "\ud800"], ids=["surrogate-escape", "lone-high"])
@pytest.mark.parametrize("input_format", [None, "stream-json"], ids=["stdin", "argv"])
async def test_prompt_refuses_surrogates_without_retaining_input(
    tmp_path, fake_transport, input_format, bad
):
    with pytest.raises(ValueError) as caught:
        request = cc.ClaudeCodeRequest(
            prompt=_SECRET + bad, input_format=input_format, repo=tmp_path
        )
        await _start(request)
    assert "surrogate" in str(caught.value).lower()
    _assert_value_free(caught.value)
    assert fake_transport == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,repeated", _TEXT_ARGUMENTS, ids=[x[0] for x in _TEXT_ARGUMENTS])
async def test_valid_utf8_text_reaches_the_transport(tmp_path, fake_transport, field, repeated):
    value = "ordinary-\u03a9-\u00e9"
    argument = ["safe", value] if repeated else value
    request = cc.ClaudeCodeRequest(prompt="hello", repo=tmp_path, **{field: argument})
    await _start(request)
    assert len(fake_transport) == 1
    cmd, _ = fake_transport[0]
    assert value in cmd


@pytest.mark.asyncio
@pytest.mark.parametrize("input_format", [None, "stream-json"], ids=["stdin", "argv"])
async def test_valid_utf8_prompt_reaches_the_transport(tmp_path, fake_transport, input_format):
    prompt = "ordinary-\u03a9-\u00e9"
    request = cc.ClaudeCodeRequest(prompt=prompt, input_format=input_format, repo=tmp_path)
    await _start(request)
    assert len(fake_transport) == 1
    cmd, kwargs = fake_transport[0]
    if input_format is None:
        assert prompt not in cmd
        assert kwargs["stdin_data"] == prompt
    else:
        assert prompt in cmd
        assert kwargs["stdin_data"] is None


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="surrogateescape path bytes are POSIX semantics")
@pytest.mark.parametrize(
    "field",
    ["system_prompt_file", "append_system_prompt_file", "mcp_config", "settings", "add_dir"],
)
async def test_surrogateescaped_path_still_reaches_transport(tmp_path, fake_transport, field):
    path = os.fsdecode(b"folder-\xff")
    argument = [path] if field == "add_dir" else path
    request = cc.ClaudeCodeRequest(prompt="hello", repo=tmp_path, **{field: argument})
    await _start(request)
    assert len(fake_transport) == 1
    cmd, _ = fake_transport[0]
    assert path in cmd
    assert os.fsencode(path) == b"folder-\xff"
