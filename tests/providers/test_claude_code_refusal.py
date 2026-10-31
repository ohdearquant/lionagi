# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A Claude Code refusal, such as a signed-in account's session limit, is said on stdout as an
error result, and the CLI then exits nonzero with nothing on stderr. The exit alone reads as a
crash; the raised error carries what the CLI said, classified, so a quota refusal is one."""

from __future__ import annotations

import json
import sys

import pytest

from lionagi._provider_errors import (
    ProviderError,
    ProviderQuotaError,
    classify_provider_error,
)
from lionhub.providers.anthropic import claude_code as cc

REFUSAL = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "api_error_status": 429,
    "result": "You've hit your session limit · resets 3am",
}


def _cli(tmp_path, lines: list[dict], code: int) -> str:
    """A stand-in for the CLI: prints each line as the real one does, then exits with `code`."""
    script = tmp_path / "claude"
    prints = "".join(f"print({json.dumps(json.dumps(line))}, flush=True)\n" for line in lines)
    script.write_text(f"#!{sys.executable}\nimport sys\n{prints}sys.exit({code})\n")
    script.chmod(0o755)
    return str(script)


async def _drain(tmp_path) -> list[dict]:
    return [
        obj async for obj in cc._ndjson_from_cli(cc.ClaudeCodeRequest(prompt="go", repo=tmp_path))
    ]


@pytest.mark.asyncio
async def test_a_session_limit_is_raised_as_a_quota_error_carrying_what_the_cli_said(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: _cli(tmp_path, [REFUSAL], 1))

    with pytest.raises(ProviderQuotaError) as info:
        await _drain(tmp_path)

    text = str(info.value)
    assert "hit your session limit" in text
    assert "api_error_status 429" in text
    assert "exited with code 1" in text  # the exit stays beside what was said
    assert type(info.value.__cause__) is RuntimeError


@pytest.mark.asyncio
async def test_a_nonzero_exit_without_an_error_result_raises_as_it_did(tmp_path, monkeypatch):
    done = {**REFUSAL, "is_error": False, "result": "done"}
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: _cli(tmp_path, [done], 1))

    with pytest.raises(RuntimeError, match="exited with code 1") as info:
        await _drain(tmp_path)
    assert not isinstance(info.value, ProviderError)


@pytest.mark.asyncio
async def test_a_later_success_result_clears_an_earlier_refusal(tmp_path, monkeypatch):
    done = {**REFUSAL, "is_error": False, "result": "completed"}
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: _cli(tmp_path, [REFUSAL, done], 1))

    with pytest.raises(RuntimeError, match="exited with code 1") as info:
        await _drain(tmp_path)
    assert not isinstance(info.value, ProviderError)


@pytest.mark.asyncio
async def test_an_error_result_on_a_clean_exit_is_left_to_the_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: _cli(tmp_path, [REFUSAL], 0))

    got = await _drain(tmp_path)

    assert got[-1]["is_error"] is True  # the session's error chunk reports it, as before


def test_the_refusal_text_alone_classifies_as_a_quota_error():
    text = "You've hit your session limit · resets 3am"
    assert isinstance(classify_provider_error(text), ProviderQuotaError)


@pytest.mark.parametrize(
    ("text", "status", "quota"),
    [
        ("Request refused", 429, True),
        # the text names the limit, the status says otherwise: the status decides
        ("Unrelated failure. The example answer quotes: You've hit your session limit", 500, False),
    ],
)
def test_a_reported_status_decides_quota_over_the_text(text, status, quota):
    assert isinstance(classify_provider_error(text, status=status), ProviderQuotaError) is quota


@pytest.mark.asyncio
async def test_a_429_status_is_a_quota_error_whatever_the_text_says(tmp_path, monkeypatch):
    terse = {**REFUSAL, "result": "Request refused"}
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: _cli(tmp_path, [terse], 1))

    with pytest.raises(ProviderQuotaError, match="api_error_status 429"):
        await _drain(tmp_path)


QUOTES_THE_STATUS = {
    **REFUSAL,
    "api_error_status": 500,
    "result": "Unrelated failure. The model's example answer quotes api_error_status 429.",
}


@pytest.mark.asyncio
async def test_a_reply_that_quotes_a_429_is_not_a_quota_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: _cli(tmp_path, [QUOTES_THE_STATUS], 1))

    with pytest.raises(ProviderError) as info:
        await _drain(tmp_path)
    assert not isinstance(info.value, ProviderQuotaError)
    assert "api_error_status 500" in str(info.value)


@pytest.mark.asyncio
async def test_an_error_chunk_that_quotes_a_429_is_not_a_quota_error(tmp_path, monkeypatch):
    """A clean exit leaves the error result to the session's error chunk, which carries the
    result's own status for the run to classify by; text that quotes the status is not the status."""
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: _cli(tmp_path, [QUOTES_THE_STATUS], 0))
    endpoint = cc.ClaudeCodeCLIEndpoint()

    chunks = [c async for c in endpoint.stream(cc.ClaudeCodeRequest(prompt="go", repo=tmp_path))]

    errors = [c for c in chunks if c.type == "error"]
    assert len(errors) == 1
    assert errors[0].metadata == {"error_status": 500}
    assert not isinstance(classify_provider_error(errors[0].content), ProviderQuotaError)


@pytest.mark.asyncio
async def test_a_clean_exits_error_chunk_carries_a_429_status(tmp_path, monkeypatch):
    terse = {**REFUSAL, "result": "Request refused"}
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: _cli(tmp_path, [terse], 0))
    endpoint = cc.ClaudeCodeCLIEndpoint()

    chunks = [c async for c in endpoint.stream(cc.ClaudeCodeRequest(prompt="go", repo=tmp_path))]

    assert [c.metadata for c in chunks if c.type == "error"] == [{"error_status": 429}]
