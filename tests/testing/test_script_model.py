# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``lionagi.testing._script.ScriptModel``."""

from __future__ import annotations

import json

import pytest

from lionagi.testing import (
    ScriptExhaustedError,
    ScriptModel,
    TextResponse,
    ToolCallResponse,
)


def _payload(prompt: str = "hi", tools: list[dict] | None = None) -> dict:
    return {
        "model": "scripted-test",
        "messages": [{"role": "user", "content": prompt}],
        "tools": tools or [],
    }


class TestPositionalMatching:
    def test_consumes_in_order(self):
        script = ScriptModel.from_responses(
            [
                {"type": "text", "content": "first"},
                {"type": "text", "content": "second"},
                {"type": "text", "content": "third"},
            ]
        )
        first, m1 = script.next(_payload(), 0)
        second, m2 = script.next(_payload(), 1)
        third, m3 = script.next(_payload(), 2)

        assert (first.content, second.content, third.content) == ("first", "second", "third")
        assert m1 == m2 == m3 == "positional"

    def test_exhaustion_raises(self):
        script = ScriptModel.from_responses([{"type": "text", "content": "only"}])
        script.next(_payload(), 0)
        with pytest.raises(ScriptExhaustedError):
            script.next(_payload(), 1)

    def test_reset_replays(self):
        script = ScriptModel.from_responses([{"type": "text", "content": "x"}])
        script.next(_payload(), 0)
        script.reset()
        entry, _ = script.next(_payload(), 0)
        assert entry.content == "x"


class TestWhenMatching:
    def test_prompt_contains_matches_out_of_order(self):
        script = ScriptModel.from_responses(
            [
                {"type": "text", "content": "default-1"},
                {
                    "type": "text",
                    "content": "weather-response",
                    "when": {"prompt_contains": "weather"},
                },
                {"type": "text", "content": "default-2"},
            ]
        )
        # First call asks about weather → when: matcher wins, positional is
        # skipped over because that entry has when:.
        weather, why = script.next(_payload("what is the weather?"), 0)
        assert weather.content == "weather-response"
        assert "prompt_contains" in why

        # Next call: no weather → positional cursor at index 0 (default-1).
        d1, _ = script.next(_payload("hi"), 1)
        assert d1.content == "default-1"

    def test_prompt_regex(self):
        script = ScriptModel.from_responses(
            [
                {
                    "type": "text",
                    "content": "matched",
                    "when": {"prompt_regex": r"^(hello|hi)\s+world$"},
                }
            ]
        )
        entry, why = script.next(_payload("hello world"), 0)
        assert entry.content == "matched"
        assert "prompt_regex" in why

    def test_has_tool(self):
        script = ScriptModel.from_responses(
            [
                {
                    "type": "tool_call",
                    "name": "lookup",
                    "arguments": {},
                    "when": {"has_tool": "lookup"},
                }
            ]
        )
        payload = _payload("anything", tools=[{"function": {"name": "lookup"}}])
        entry, _ = script.next(payload, 0)
        assert isinstance(entry, ToolCallResponse)

    def test_call_index_matcher(self):
        script = ScriptModel.from_responses(
            [
                {"type": "text", "content": "first-only", "when": {"call_index": 0}},
                {"type": "text", "content": "default"},
            ]
        )
        first, _ = script.next(_payload(), 0)
        assert first.content == "first-only"
        second, _ = script.next(_payload(), 1)
        assert second.content == "default"

    def test_when_only_serves_each_matcher_at_most_once(self):
        script = ScriptModel.from_responses(
            [
                {
                    "type": "text",
                    "content": "matched",
                    "when": {"prompt_contains": "X"},
                }
            ]
        )
        entry, _ = script.next(_payload("contains X please"), 0)
        assert entry.content == "matched"
        # No fallback positional entries — second call exhausted.
        with pytest.raises(ScriptExhaustedError):
            script.next(_payload("contains X again"), 1)


class TestLoaders:
    def test_from_json(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "responses": [{"type": "text", "content": "from-json"}],
                }
            )
        )
        script = ScriptModel.from_json(path)
        entry, _ = script.next(_payload(), 0)
        assert isinstance(entry, TextResponse)
        assert entry.content == "from-json"

    def test_from_yaml_requires_pyyaml_or_works(self, tmp_path):
        pytest.importorskip("yaml")
        path = tmp_path / "s.yaml"
        path.write_text("version: 1\nresponses:\n  - type: text\n    content: from-yaml\n")
        script = ScriptModel.from_yaml(path)
        entry, _ = script.next(_payload(), 0)
        assert entry.content == "from-yaml"

    def test_coerce_from_list(self):
        script = ScriptModel.coerce([{"type": "text", "content": "list-coerced"}])
        entry, _ = script.next(_payload(), 0)
        assert entry.content == "list-coerced"

    def test_coerce_passes_through_scriptmodel(self):
        original = ScriptModel.from_responses([{"type": "text", "content": "x"}])
        again = ScriptModel.coerce(original)
        assert again is original

    def test_invalid_response_type_raises(self):
        with pytest.raises(ValueError, match="unknown response type"):
            ScriptModel.from_responses([{"type": "made_up"}])

    def test_unknown_field_rejected(self):
        # Strict mode: extra fields on response entries are a typo, not silent.
        with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError or ValueError
            ScriptModel.from_responses([{"type": "text", "content": "x", "typo": "oops"}])
