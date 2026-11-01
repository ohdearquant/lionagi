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

from lionagi.service.types.stream_chunk import StreamChunk
from lionhub.providers.openai.codex import (
    CODEX_TOOL_OUTPUT_MAX_CHARS,
    CodexCodeRequest,
    stream_codex_cli,
)

HALF = CODEX_TOOL_OUTPUT_MAX_CHARS // 2
MARKER = "\n[output truncated: "


def _fake_events(events: list[dict]):
    async def fake(request):
        for event in events:
            yield event

    return patch(
        "lionhub.providers.openai.codex.stream_codex_cli_events",
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
async def test_a_long_list_of_short_arguments_keeps_the_items_that_fit_and_says_how_many_were_left_out():
    arguments = {"ids": list(range(200_000)), "limit": 5}

    chunks, uses, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "fetch", "arguments": json.dumps(arguments)}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    decoded = json.loads(echoed)
    assert len(echoed) <= CODEX_TOOL_OUTPUT_MAX_CHARS + 100
    assert decoded["limit"] == 5
    *kept, note = decoded["ids"]
    assert kept == list(range(len(kept)))
    assert note == f"[{200_000 - len(kept):,} of 200,000 items not shown]"
    assert uses[0]["input"] == echoed


@pytest.mark.asyncio
async def test_a_list_keeps_its_first_item_cut_when_that_item_alone_is_over_the_bound():
    arguments = {"queries": ["q" * 1_000_000, "r" * 1_000_000]}

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "search", "arguments": arguments}
    )

    first, note = next(c for c in chunks if c.type == "tool_use").tool_input["queries"]
    assert _parts(first)[2] == 1_000_000
    assert note == "[1 of 2 items not shown]"


@pytest.mark.asyncio
async def test_json_arguments_nested_too_deep_to_decode_are_cut_as_text():
    arguments = "[" * 5_000 + '"' + "q" * 30_000 + '"' + "]" * 5_000

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "search", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    head, omitted, total, _ = _parts(echoed)
    assert head == arguments[:HALF]
    assert total == len(arguments)


@pytest.mark.asyncio
async def test_a_tool_use_block_in_a_message_echoes_its_input_bounded():
    request = CodexCodeRequest(prompt="test", verbose_output=False)
    block = {
        "type": "tool_use",
        "id": "tu-1",
        "name": "search",
        "input": {"query": "q" * 1_000_000},
    }
    uses: list[dict] = []
    chunks: list[StreamChunk] = []
    with _fake_events([{"type": "message", "content": [block]}]):
        async for out in stream_codex_cli(request, on_tool_use=uses.append):
            if isinstance(out, StreamChunk) and out.type == "tool_use":
                chunks.append(out)

    echoed = chunks[0].tool_input
    assert _parts(echoed["query"])[2] == 1_000_000
    assert uses[0]["input"] == echoed


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


def _nested(depth: int, leaf, kind: str = "dict"):
    value = leaf
    for _ in range(depth):
        value = {"k": value} if kind == "dict" else [value]
    return value


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        "plain",
        'quote " backslash \\ newline \n tab \t nul \x00',
        "漢字 é \U0001f468\u200d\U0001f469\u200d\U0001f467",
        [1, 2.5, None, True, False, float("inf"), -0.0, 10**30],
        {"a": [1, {"b": []}], "é": "x\ny", 1: "one", 2.5: [], None: {}, False: "f"},
        [[[[[]]]], {"k": {"k": {"k": "v"}}}],
        {"path": __import__("pathlib").PurePosixPath("/x/y")},
    ],
)
def test_the_json_size_of_a_value_is_the_length_of_its_json_text(value):
    from lionhub.providers.openai.codex import _json_size

    assert _json_size(value, {}) == len(json.dumps(value, ensure_ascii=False, default=str))


@pytest.mark.asyncio
@pytest.mark.parametrize("as_text", [False, True], ids=["mapping", "json-text"])
async def test_a_mapping_of_many_long_values_is_bounded_as_a_whole(as_text):
    arguments = {f"k{i}": "v" * 19_999 for i in range(60)}

    chunks, _, _ = await _run(
        {
            "type": "function_call",
            "id": "fc-1",
            "name": "write",
            "arguments": json.dumps(arguments) if as_text else arguments,
        }
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    decoded = json.loads(echoed) if as_text else echoed
    assert len(json.dumps(decoded, ensure_ascii=False)) <= CODEX_TOOL_OUTPUT_MAX_CHARS + 200
    assert list(decoded) == ["k0", "[omitted]"]
    assert _parts(decoded["k0"])[2] == 19_999
    assert decoded["[omitted]"] == "59 of 60 keys not shown"


@pytest.mark.asyncio
async def test_small_entries_beside_a_long_one_are_kept_whole_in_their_place():
    arguments = {"limit": 5, "query": "q" * 1_000_000, "paths": ["a.py", "b.py"]}

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "search", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    assert list(echoed) == ["limit", "query", "paths"]
    assert (echoed["limit"], echoed["paths"]) == (5, ["a.py", "b.py"])
    assert _parts(echoed["query"])[2] == 1_000_000


@pytest.mark.asyncio
async def test_a_mapping_that_is_the_first_item_of_a_list_is_bounded_too():
    arguments = json.dumps({"items": [{f"k{i}": i for i in range(100_000)}, 1]})

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "put", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    assert len(echoed) <= CODEX_TOOL_OUTPUT_MAX_CHARS + 200
    first, note = json.loads(echoed)["items"]
    kept = len(first) - 1
    assert first["[omitted]"] == f"{100_000 - kept:,} of 100,000 keys not shown"
    assert [first[f"k{i}"] for i in range(kept)] == list(range(kept))
    assert note == "[1 of 2 items not shown]"


@pytest.mark.asyncio
async def test_a_list_cuts_the_first_item_that_does_not_fit_and_stops_there():
    arguments = {"items": ["a" * 100, "b" * 30_000, "c" * 100]}

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "put", "arguments": arguments}
    )

    first, second, note = next(c for c in chunks if c.type == "tool_use").tool_input["items"]
    assert first == "a" * 100
    assert _parts(second)[2] == 30_000
    assert note == "[1 of 3 items not shown]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"k0": "\x01" * 30_000},
        json.dumps({"k0": '"' * 30_000}),
        ["\n" * 30_000],
    ],
    ids=["control-characters", "quotes-in-json-text", "newlines-in-a-list"],
)
async def test_strings_are_cut_by_their_length_as_json_writes_them(arguments):
    """A control character is six characters of JSON and a quote or a newline two, so a
    string cut by its characters alone would echo several times the bound."""
    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "put", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    text = echoed if isinstance(echoed, str) else json.dumps(echoed, ensure_ascii=False)
    assert len(text) <= CODEX_TOOL_OUTPUT_MAX_CHARS + 200
    if isinstance(echoed, str):
        assert json.loads(echoed)


@pytest.mark.asyncio
async def test_a_file_change_path_of_control_characters_is_cut_by_its_json_length():
    changes = [{"path": "\x01" * 30_000, "kind": "add"}]

    chunks, _, _ = await _run(
        {"type": "file_change", "id": "fc-1", "changes": changes, "status": "completed"}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input["changes"]
    assert len(json.dumps(echoed, ensure_ascii=False)) <= CODEX_TOOL_OUTPUT_MAX_CHARS + 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["a" * 20_000 + "\x01" * 20_000, "\x01" * 20_000 + "a" * 20_000],
    ids=["escapes-at-the-end", "escapes-at-the-start"],
)
async def test_the_head_and_the_tail_of_a_cut_string_are_measured_apart(text):
    """Each end keeps as much as fits its half of the room: a plain end about 10,000
    characters, and an end of control characters about a sixth of that."""
    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "put", "arguments": {"k0": text}}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    assert len(json.dumps(echoed, ensure_ascii=False)) <= CODEX_TOOL_OUTPUT_MAX_CHARS + 200
    head, _, total, tail = _parts(echoed["k0"])
    plain, escaped = (head, tail) if text[0] == "a" else (tail, head)
    assert total == 40_000
    assert set(plain) == {"a"} and len(plain) > 9_000
    assert set(escaped) == {"\x01"} and 1_000 < len(escaped) < 2_000


@pytest.mark.asyncio
async def test_a_key_is_measured_as_json_writes_it():
    """Five thousand control characters are thirty thousand characters of JSON, so a key
    measured by its characters would be kept and the echo would be over the bound."""
    arguments = {"\x01" * 5_000: "v", "path": "a.txt"}

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "put", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    assert echoed == {"path": "a.txt", "[omitted]": "1 of 2 keys not shown"}


@pytest.mark.asyncio
async def test_an_entry_cut_to_fit_keeps_its_note_within_the_bound():
    """The value of an entry cut to fit is given the room less its note's share, so the
    entry, its cut line included, stays within the bound."""
    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "put", "arguments": {"a": "x" * 30_000}}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    assert len(json.dumps(echoed, ensure_ascii=False)) <= CODEX_TOOL_OUTPUT_MAX_CHARS
    assert _parts(echoed["a"])[2] == 30_000


@pytest.mark.asyncio
async def test_a_list_item_cut_to_fit_keeps_its_note_within_the_bound():
    """The item a list cuts is given the room left less its note's share, so the list, the
    item's cut line included, stays within the bound, with only the list's own count note on
    top."""
    arguments = ["a" * 100, "x" * 30_000]

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "put", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    assert len(json.dumps(echoed, ensure_ascii=False)) <= CODEX_TOOL_OUTPUT_MAX_CHARS
    assert echoed[0] == "a" * 100
    assert _parts(echoed[1])[2] == 30_000


@pytest.mark.asyncio
@pytest.mark.parametrize("leaf", ["q" * 25_000, "q"], ids=["long-leaf", "short-leaf"])
async def test_a_list_of_changes_nested_too_deep_to_measure_is_echoed_bounded(leaf):
    changes = _nested(5_000, leaf, "list")

    chunks, _, _ = await _run(
        {"type": "file_change", "id": "fc-1", "changes": changes, "status": "completed"}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input["changes"]
    if echoed is not changes:
        assert isinstance(echoed, str)
        assert len(echoed) <= CODEX_TOOL_OUTPUT_MAX_CHARS + 200
    else:
        assert len(json.dumps(changes)) <= CODEX_TOOL_OUTPUT_MAX_CHARS


@pytest.mark.asyncio
async def test_an_entry_whose_key_does_not_fit_is_left_out_whole():
    """A key is never cut: a redactor classifies an entry by its key, and a cut can drop the
    part that names a credential, so the entry is left out and counted."""
    from lionagi.studio.operator.redact import redact_arguments

    marked = "k" * 15_000 + "password" + "k" * 15_000
    arguments = {marked: "s3cr3t-value", "path": "a.txt"}

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "put", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    assert echoed == {"path": "a.txt", "[omitted]": "1 of 2 keys not shown"}
    assert "s3cr3t-value" not in json.dumps(redact_arguments(echoed))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dict", "list"])
@pytest.mark.parametrize("leaf", ["q" * 25_000, "q"], ids=["long-leaf", "short-leaf"])
async def test_arguments_nested_too_deep_to_measure_are_echoed_bounded(kind, leaf):
    """No exception leaves the stream: the arguments are bounded as a result is, their JSON
    text kept when within the bound and cut when over it. Where this Python's JSON encoder
    cannot go this deep (how deep it goes depends on the Python version and the stack), a line
    says they are nested too deep to show."""
    arguments = _nested(5_000, leaf, kind)

    chunks, uses, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "put", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    if echoed is not arguments:
        assert isinstance(echoed, str)
        assert len(echoed) <= CODEX_TOOL_OUTPUT_MAX_CHARS + 200
    else:
        assert len(json.dumps(arguments)) <= CODEX_TOOL_OUTPUT_MAX_CHARS
    assert uses[0]["input"] is echoed


@pytest.mark.asyncio
async def test_a_structured_result_nested_very_deep_is_recorded_as_bounded_text():
    output = _nested(5_000, "q" * 25_000)

    chunks, callback = await _tool_results(
        {"type": "function_call_output", "call_id": "call-1", "output": output}
    )

    kept = chunks[0].tool_output
    assert isinstance(kept, str)
    assert len(kept) <= CODEX_TOOL_OUTPUT_MAX_CHARS + 200
    assert callback[0]["content"] == kept


@pytest.mark.asyncio
async def test_a_structured_result_the_json_encoder_cannot_reach_says_it_is_too_deep(
    monkeypatch,
):
    """Where this Python's JSON encoder cannot go as deep as a result (how deep it goes depends
    on the Python version and the stack; the encoder is stubbed here), the result is recorded
    as a line saying it is nested too deep to show."""
    import types

    from lionhub.providers.openai import codex

    def dumps(obj, *args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded while encoding a JSON object")

    monkeypatch.setattr(codex, "json", types.SimpleNamespace(dumps=dumps, loads=json.loads))

    chunks, callback = await _tool_results(
        {"type": "function_call_output", "call_id": "call-1", "output": {"rows": [1, 2]}}
    )

    assert chunks[0].tool_output == "[dict nested too deep to show]"
    assert callback[0]["content"] == chunks[0].tool_output


@pytest.mark.asyncio
async def test_a_single_file_change_over_the_bound_keeps_its_entry_cut():
    changes = [{"kind": "add", "path": "p" * 30_000}]

    chunks, _, _ = await _run(
        {"type": "file_change", "id": "fc-1", "changes": changes, "status": "completed"}
    )

    (entry,) = next(c for c in chunks if c.type == "tool_use").tool_input["changes"]
    assert entry["kind"] == "add"
    assert _parts(entry["path"])[2] == 30_000


@pytest.mark.asyncio
async def test_file_changes_given_as_a_mapping_stay_a_mapping():
    changes = {"a.py": "x" * 30_000, "b.py": "y"}

    chunks, _, _ = await _run(
        {"type": "file_change", "id": "fc-1", "changes": changes, "status": "completed"}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input["changes"]
    assert isinstance(echoed, dict)
    assert echoed["b.py"] == "y"
    assert _parts(echoed["a.py"])[2] == 30_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base,mark",
    [("\U0001f44d", "\U0001f3fd"), ("\u2764", "\ufe0f"), ("\u2764", "\ufe0e")],
    ids=["skin-tone", "emoji-selector", "text-selector"],
)
async def test_a_cut_keeps_a_modifier_or_selector_with_its_character(base, mark):
    output = "a" * (HALF - 1) + base + mark + "x" * 5_000 + "b" * HALF
    assert output[HALF] == mark

    chunks, _ = await _tool_results(_command(output))

    head, _, _, _ = _parts(chunks[0].tool_output)
    assert head == "a" * (HALF - 1)


@pytest.mark.asyncio
async def test_json_text_naming_a_string_is_cut_as_text_not_encoded_again():
    arguments = json.dumps("q" * 30_000)

    chunks, _, _ = await _run(
        {"type": "function_call", "id": "fc-1", "name": "put", "arguments": arguments}
    )

    echoed = next(c for c in chunks if c.type == "tool_use").tool_input
    head, _, total, _ = _parts(echoed)
    assert head == arguments[:HALF]
    assert total == len(arguments)


def test_a_deep_value_is_serialized_a_bounded_number_of_times(monkeypatch):
    """Each mapping and list is measured once, so bounding a value nested many levels
    deep costs a small multiple of its size, not its size per level."""
    import types

    from lionhub.providers.openai import codex

    leaf = "q" * 200_000
    value = leaf
    for _ in range(50):
        value = {"k": value, "n": 1}
    serialized = 0

    def dumps(obj, *args, **kwargs):
        nonlocal serialized
        out = json.dumps(obj, *args, **kwargs)
        serialized += len(out)
        return out

    monkeypatch.setattr(codex, "json", types.SimpleNamespace(dumps=dumps, loads=json.loads))

    bounded = codex._bounded_leaves(value)

    assert serialized < 4 * len(leaf)
    assert len(json.dumps(bounded, ensure_ascii=False)) <= CODEX_TOOL_OUTPUT_MAX_CHARS + 200
