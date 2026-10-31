# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Synthetic regression checks for JSON response-format prompt rendering."""

from __future__ import annotations

import json

import pytest
import yaml
from pydantic import BaseModel

from lionagi.operations.schema.json_structure import JsonStructure
from lionagi.protocols.messages.instruction import InstructionContent


class ExampleResponse(BaseModel):
    answer: str
    count: int


_STRING_RULE = (
    "Do not put triple backticks inside JSON string values. "
    "Escape quotes and special characters inside JSON strings."
)
_DICT_FORMAT = {
    "answer": 'A synthetic "quote"\nand a second line',
    "nested": {"items": ["one", "two"]},
}


def _assert_fence_contract(rendered: str) -> list[str]:
    lines = rendered.splitlines()
    assert lines.count("```json") == 1
    assert lines.count("```") == 1
    opening = lines.index("```json")
    closing = lines.index("```")
    assert opening < closing
    assert closing + 1 < len(lines)
    assert lines[closing + 1] == _STRING_RULE
    assert "No triple backticks." not in rendered
    return lines[opening + 1 : closing]


@pytest.mark.parametrize("response_format", [_DICT_FORMAT, ExampleResponse], ids=["dict", "model"])
def test_json_structure_fence_and_scoped_string_guidance(response_format):
    rendered = JsonStructure(response_format).render()
    fenced_example = _assert_fence_contract(rendered)
    if isinstance(response_format, dict):
        assert json.loads("\n".join(fenced_example)) == response_format


@pytest.mark.parametrize("response_format", [_DICT_FORMAT, ExampleResponse], ids=["dict", "model"])
def test_instruction_yaml_preserves_response_format_fence_contract(response_format):
    direct = JsonStructure(response_format).render()
    instruction = 'A synthetic "instruction"\nwith a second line'
    guidance = 'A synthetic "guidance"\nwith another line'
    content = InstructionContent(
        instruction=instruction,
        guidance=guidance,
        response_format=response_format,
    )
    rendered = content.rendered
    assert isinstance(rendered, str)
    parsed = yaml.safe_load(rendered)
    assert parsed["Instruction"] == instruction
    assert parsed["Guidance"] == guidance
    assert parsed["ResponseFormat"] == direct
    _assert_fence_contract(parsed["ResponseFormat"])


def test_empty_json_structure_renders_no_response_format():
    assert JsonStructure({}).render() == ""
    assert JsonStructure._format_response_format(None) == ""
    for response_format in (None, {}):
        content = InstructionContent(
            instruction="a synthetic instruction", response_format=response_format
        )
        parsed = yaml.safe_load(content.rendered)
        assert parsed["Instruction"] == "a synthetic instruction"
        assert "ResponseFormat" not in parsed
