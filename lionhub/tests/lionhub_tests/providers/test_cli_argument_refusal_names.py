# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Command-line refusals never name a preceding user value as a flag."""

import pytest

from lionhub.providers.anthropic import claude_code as claude
from lionhub.providers.openai import codex

_SECRET = "-SECRET-TOKEN-zz9"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["y" * 1_048_576, "bad\x00value"], ids=["oversize", "nul"])
async def test_claude_repeated_tool_value_is_not_named_as_a_flag(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(claude, "resolve_claude_cli", lambda: "claude")
    request = claude.ClaudeCodeRequest(
        repo=tmp_path, prompt="hello", disallowed_tools=[_SECRET, bad]
    )
    with pytest.raises(ValueError) as caught:
        async for _ in claude._ndjson_from_cli(request):
            pass
    assert _SECRET not in str(caught.value)
    assert "Command-line argument" in str(caught.value)


@pytest.mark.asyncio
async def test_claude_names_its_own_declared_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "resolve_claude_cli", lambda: "claude")
    request = claude.ClaudeCodeRequest(repo=tmp_path, prompt="hello", system_prompt="bad\x00value")
    with pytest.raises(ValueError, match="value of --system-prompt"):
        async for _ in claude._ndjson_from_cli(request):
            pass


@pytest.mark.asyncio
async def test_claude_does_not_name_a_codex_flag_used_as_a_value(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "resolve_claude_cli", lambda: "claude")
    request = claude.ClaudeCodeRequest(
        repo=tmp_path, prompt="hello", disallowed_tools=["--sandbox", "bad\x00value"]
    )
    with pytest.raises(ValueError) as caught:
        async for _ in claude._ndjson_from_cli(request):
            pass
    assert "--sandbox" not in str(caught.value)
    assert "Command-line argument" in str(caught.value)


@pytest.mark.asyncio
async def test_claude_known_flag_spelling_as_list_value_does_not_leak_other_values(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(claude, "resolve_claude_cli", lambda: "claude")
    oversized = "y" * 1_048_576
    request = claude.ClaudeCodeRequest(
        repo=tmp_path, prompt="hello", disallowed_tools=[_SECRET, "--model", oversized]
    )
    with pytest.raises(ValueError) as caught:
        async for _ in claude._ndjson_from_cli(request):
            pass
    message = str(caught.value)
    assert "value of --model" in message or "Command-line argument" in message
    assert _SECRET not in message
    assert "y" * 100 not in message


@pytest.mark.parametrize("bad", ["y" * 1_048_576, "bad\x00value"], ids=["oversize", "nul"])
def test_codex_value_is_not_named_as_a_flag(bad):
    with pytest.raises(ValueError) as caught:
        codex._refuse_oversized_command(["codex", "--add-dir", _SECRET, bad])
    assert _SECRET not in str(caught.value)
    assert "Command-line argument" in str(caught.value)


@pytest.mark.parametrize("previous", ["--disallowedTools", "--unknown", _SECRET])
def test_codex_does_not_use_unrelated_or_unknown_flags(previous):
    with pytest.raises(ValueError) as caught:
        codex._refuse_oversized_command(["codex", previous, "bad\x00value"])
    assert previous not in str(caught.value)
    assert "Command-line argument" in str(caught.value)


def test_codex_still_names_its_known_flag_and_keeps_override_values_private():
    with pytest.raises(ValueError, match="value of --sandbox"):
        codex._refuse_oversized_command(["codex", "--sandbox", "bad\x00value"])
    with pytest.raises(ValueError) as caught:
        codex._refuse_oversized_command(["codex", "-c", f"{_SECRET}\x00value"])
    assert "-c override at argument 2" in str(caught.value)
    assert _SECRET not in str(caught.value)
