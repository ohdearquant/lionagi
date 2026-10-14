# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A result this provider records from a Codex tool call, and the command, file change or
function call arguments it echoes as the call's input, are bounded, and the bound is stated
where the cut is.

No Codex thread is resumed: every turn of a Codex branch renders the branch's history into its
prompt, so a result recorded whole is rendered into later prompts. The cut keeps the head and
the tail and names how much is missing, because a result that is silently partial reads as a
complete one.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from lionagi.providers.openai.codex import (
    CODEX_TOOL_OUTPUT_MAX_CHARS,
    CodexCodeRequest,
    stream_codex_cli,
)
from lionagi.service.types.stream_chunk import StreamChunk

HALF = CODEX_TOOL_OUTPUT_MAX_CHARS // 2
MARKER = "\n[output truncated: "


def _fake_events(events: list[dict]):
    async def fake(request):
        for event in events:
            yield event

    return patch(
        "lionagi.providers.openai.codex.stream_codex_cli_events",
        side_effect=fake,
    )


async def _run(item: dict) -> tuple[list[StreamChunk], list[dict], list[dict]]:
    request = CodexCodeRequest(prompt="test", verbose_output=False)
    uses: list[dict] = []
    results: list[dict] = []
    chunks: list[StreamChunk] = []
    with _fake_events([{"type": "item.completed", "item": item}]):
        async for out in stream_codex_cli(
            request, on_tool_use=uses.append, on_tool_result=results.append
        ):
            if isinstance(out, StreamChunk) and out.type in ("tool_use", "tool_result"):
                chunks.append(out)
    return chunks, uses, results


async def _tool_results(item: dict) -> tuple[list[StreamChunk], list[dict]]:
    chunks, _, results = await _run(item)
    return [c for c in chunks if c.type == "tool_result"], results


def _command(output: str, command: str = "rg -n pattern .") -> dict:
    return {
        "type": "command_execution",
        "id": "cmd-1",
        "command": command,
        "aggregated_output": output,
        "exit_code": 0,
        "status": "completed",
    }


def _parts(kept: str) -> tuple[str, int, int, str]:
    """Split a cut result into its head, the omitted and total counts, and its tail."""
    head, rest = kept.split(MARKER, 1)
    counts, tail = rest.split(" characters omitted here]\n", 1)
    omitted, total = (int(n.replace(",", "")) for n in counts.split(" of "))
    return head, omitted, total, tail


@pytest.mark.asyncio
async def test_a_command_result_over_the_bound_keeps_head_and_tail_and_says_what_was_cut():
    head = "HEAD-LINE\n" + "a" * 30_000
    tail = "b" * 30_000 + "\nTAIL-LINE"
    output = head + "x" * 1_000_000 + tail

    chunks, callback = await _tool_results(_command(output))

    kept = chunks[0].tool_output
    assert kept.startswith("HEAD-LINE\n")
    assert kept.endswith("\nTAIL-LINE")
    omitted = len(output) - 2 * HALF
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
async def test_a_structured_function_call_output_within_the_bound_is_left_as_it_came():
    output = [{"type": "text", "text": "e" * 100}]

    chunks, callback = await _tool_results(
        {"type": "function_call_output", "call_id": "call-1", "output": output}
    )

    assert chunks[0].tool_output is output
    assert callback[0]["content"] is output


@pytest.mark.asyncio
async def test_a_structured_function_call_output_over_the_bound_is_cut_as_its_json_text():
    output = [{"type": "text", "text": "e" * 1_000_000}]
    text = json.dumps(output, ensure_ascii=False)

    chunks, callback = await _tool_results(
        {"type": "function_call_output", "call_id": "call-1", "output": output}
    )

    kept = chunks[0].tool_output
    head, omitted, total, tail = _parts(kept)
    assert head == text[:HALF]
    assert tail == text[-HALF:]
    assert (omitted, total) == (len(text) - 2 * HALF, len(text))
    assert callback[0]["content"] == kept


@pytest.mark.asyncio
async def test_long_json_arguments_are_bounded_and_still_parse():
    arguments = '{"query": "' + "q" * 1_000_000 + '", "limit": 5}'

    chunks, uses, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "search", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    decoded = json.loads(echoed)
    assert decoded["limit"] == 5
    assert _parts(decoded["query"])[2] == 1_000_000
    assert uses[0]["input"] == echoed


@pytest.mark.asyncio
async def test_long_arguments_that_are_not_json_are_cut_as_text():
    arguments = "q" * 1_000_000

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "search", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    head, omitted, total, _ = _parts(echoed)
    assert head == arguments[:HALF]
    assert (omitted, total) == (len(arguments) - 2 * HALF, len(arguments))


@pytest.mark.asyncio
async def test_structured_function_call_arguments_keep_their_shape_when_cut():
    arguments = {"query": "q" * 1_000_000, "limit": 5, "paths": ["a.py", "b.py"]}

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "search", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    assert set(echoed) == {"query", "limit", "paths"}
    assert (echoed["limit"], echoed["paths"]) == (5, ["a.py", "b.py"])
    assert _parts(echoed["query"])[2] == 1_000_000


@pytest.mark.asyncio
async def test_function_call_arguments_within_the_bound_are_left_as_they_came():
    arguments = {"query": "q" * 100, "limit": 5}

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "search", "arguments": arguments}
    )

    assert next(c for c in chunks if c.type == "tool_use").tool_input is arguments


@pytest.mark.asyncio
async def test_a_long_command_is_cut_where_it_is_echoed_as_the_calls_input():
    command = "printf '%s' " + "q" * 1_000_000

    chunks, uses, _ = await _run(_command("ok", command=command))

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input["command"]
    head, omitted, total, _ = _parts(echoed)
    assert head == command[:HALF]
    assert (omitted, total) == (len(command) - 2 * HALF, len(command))
    assert uses[0]["input"]["command"] == echoed


@pytest.mark.asyncio
async def test_a_file_change_over_the_bound_cuts_its_echo_and_its_summary():
    changes = [{"kind": "update", "path": f"src/pkg/module_{i:05d}.py"} for i in range(5_000)]

    chunks, uses, results = await _run(
        {"type": "file_change", "id": "fc-1", "changes": changes, "status": "completed"}
    )

    use = next(c for c in chunks if c.type == "tool_use")
    result = next(c for c in chunks if c.type == "tool_result")
    echoed = use.tool_input["changes"]
    # Still a list, so a reader looking for each entry's path finds the ones kept.
    *kept, marker = echoed
    assert kept == changes[: len(kept)]
    assert len(json.dumps(kept, ensure_ascii=False)) <= CODEX_TOOL_OUTPUT_MAX_CHARS
    assert len(json.dumps(changes[: len(kept) + 1], ensure_ascii=False)) > (
        CODEX_TOOL_OUTPUT_MAX_CHARS
    )
    assert marker == {"omitted": f"{len(changes) - len(kept):,} of 5,000 changes not shown"}
    assert "path" not in marker
    assert uses[0]["input"]["changes"] == echoed
    summary = "; ".join(f"update: {c['path']}" for c in changes)
    head, omitted, total, tail = _parts(result.tool_output)
    assert head == summary[:HALF]
    assert tail == summary[-HALF:]
    assert total == len(summary)
    assert results[0]["content"] == result.tool_output


@pytest.mark.asyncio
async def test_a_file_change_within_the_bound_is_echoed_and_summarized_as_before():
    changes = [{"kind": "add", "path": "a.py"}, {"kind": "update", "path": "b.py"}]

    chunks, _, _ = await _run(
        {"type": "file_change", "id": "fc-1", "changes": changes, "status": "completed"}
    )

    assert next(c for c in chunks if c.type == "tool_use").tool_input == {"changes": changes}
    assert (
        next(c for c in chunks if c.type == "tool_result").tool_output == "add: a.py; update: b.py"
    )


@pytest.mark.asyncio
async def test_a_cut_does_not_separate_a_character_from_its_marks():
    # The head's cut falls inside a joined emoji sequence, the tail's on a combining accent.
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    output = "a" * (HALF - 1) + family + "x" * 5_000 + "e\u0301" + "b" * (HALF - 1)
    assert output[HALF] == "\u200d"
    assert output[len(output) - HALF] == "\u0301"

    chunks, _ = await _tool_results(_command(output))

    head, omitted, total, tail = _parts(chunks[0].tool_output)
    assert head == "a" * (HALF - 1)
    assert tail == "b" * (HALF - 1)
    assert (omitted, total) == (len(output) - len(head) - len(tail), len(output))


@pytest.mark.asyncio
async def test_a_run_of_marks_longer_than_the_backoff_is_cut_where_the_bound_falls():
    output = "a" * (HALF - 50) + "e" + "\u0301" * 100 + "y" * 30_000

    chunks, _ = await _tool_results(_command(output))

    head, omitted, _, _ = _parts(chunks[0].tool_output)
    assert head == output[:HALF]
    assert omitted == len(output) - 2 * HALF
