# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A tool result this provider keeps is bounded, and the bound is stated in the result.

Every turn of a Codex branch sends the branch's rendered history as its input, and the
CLI refuses an input past 1,048,576 characters. One repository-wide search can return
about that much, so an uncut result recorded on one turn makes every later turn of the
branch impossible. The cut keeps the head and the tail and names how much is missing,
because a result that is silently partial reads as a complete one.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lionagi.providers.openai.codex import (
    CODEX_TOOL_OUTPUT_MAX_CHARS,
    CodexCodeRequest,
    stream_codex_cli,
)
from lionagi.service.types.stream_chunk import StreamChunk


def _fake_events(events: list[dict]):
    async def fake(request):
        for event in events:
            yield event

    return patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake,
    )


async def _tool_results(item: dict) -> tuple[list[StreamChunk], list[dict]]:
    request = CodexCodeRequest(prompt="test", verbose_output=False)
    callback: list[dict] = []
    chunks: list[StreamChunk] = []
    with _fake_events([{"type": "item.completed", "item": item}]):
        async for out in stream_codex_cli(request, on_tool_result=callback.append):
            if isinstance(out, StreamChunk) and out.type == "tool_result":
                chunks.append(out)
    return chunks, callback


def _command(output: str) -> dict:
    return {
        "type": "command_execution",
        "id": "cmd-1",
        "command": "rg -n pattern .",
        "aggregated_output": output,
        "exit_code": 0,
        "status": "completed",
    }


@pytest.mark.asyncio
async def test_a_command_result_over_the_bound_keeps_head_and_tail_and_says_what_was_cut():
    head = "HEAD-LINE\n" + "a" * 30_000
    tail = "b" * 30_000 + "\nTAIL-LINE"
    output = head + "x" * 1_000_000 + tail

    chunks, callback = await _tool_results(_command(output))

    kept = chunks[0].tool_output
    assert kept.startswith("HEAD-LINE\n")
    assert kept.endswith("\nTAIL-LINE")
    omitted = len(output) - CODEX_TOOL_OUTPUT_MAX_CHARS
    assert f"[output truncated: {omitted:,} of {len(output):,} characters omitted here]" in kept
    # Within the bound plus the one line that states the cut.
    assert len(kept) < CODEX_TOOL_OUTPUT_MAX_CHARS + 200
    assert callback[0]["content"] == kept


@pytest.mark.asyncio
async def test_a_command_result_at_the_bound_is_kept_whole():
    output = "c" * CODEX_TOOL_OUTPUT_MAX_CHARS

    chunks, callback = await _tool_results(_command(output))

    assert chunks[0].tool_output == output
    assert callback[0]["content"] == output


@pytest.mark.asyncio
async def test_the_bound_counts_characters_not_bytes():
    # Three UTF-8 bytes per character: over the bound in bytes, inside it in characters.
    output = "漢" * CODEX_TOOL_OUTPUT_MAX_CHARS

    chunks, _ = await _tool_results(_command(output))

    assert chunks[0].tool_output == output


@pytest.mark.asyncio
async def test_a_function_call_output_over_the_bound_is_cut_the_same_way():
    output = "d" * (CODEX_TOOL_OUTPUT_MAX_CHARS + 1)

    chunks, callback = await _tool_results(
        {"type": "function_call_output", "call_id": "call-1", "output": output}
    )

    kept = chunks[0].tool_output
    assert "[output truncated: 1 of" in kept
    assert callback[0]["content"] == kept


@pytest.mark.asyncio
async def test_a_structured_function_call_output_is_left_as_it_came():
    output = [{"type": "text", "text": "e" * (CODEX_TOOL_OUTPUT_MAX_CHARS + 1)}]

    chunks, _ = await _tool_results(
        {"type": "function_call_output", "call_id": "call-1", "output": output}
    )

    assert chunks[0].tool_output == output
