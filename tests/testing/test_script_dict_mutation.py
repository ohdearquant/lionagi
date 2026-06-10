# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests that ScriptModel does not mutate caller-supplied dicts."""

from __future__ import annotations

import copy

from lionagi.testing import ScriptModel, ToolCallResponse


def _payload(prompt: str = "hi") -> dict:
    return {
        "model": "scripted-test",
        "messages": [{"role": "user", "content": prompt}],
    }


class TestCallerDictUnchanged:
    """ScriptModel construction must not modify the dict the caller passed in."""

    def test_model_validate_does_not_mutate_input_dict(self):
        responses_list = [
            {"type": "text", "content": "hello"},
            {"type": "tool_call", "name": "fn", "arguments": {"x": 1}},
        ]
        caller_dict = {"version": 1, "responses": responses_list}
        snapshot = copy.deepcopy(caller_dict)

        ScriptModel.model_validate(caller_dict)

        assert caller_dict == snapshot, "model_validate mutated the caller-supplied dict"

    def test_from_responses_does_not_mutate_entry_dicts(self):
        entry = {"type": "text", "content": "original"}
        snapshot = dict(entry)

        ScriptModel.from_responses([entry])

        assert entry == snapshot, "from_responses mutated the caller-supplied entry dict"

    def test_coerce_dict_does_not_mutate_input(self):
        caller = {
            "version": 1,
            "responses": [{"type": "text", "content": "coerced"}],
        }
        snapshot = copy.deepcopy(caller)

        ScriptModel.coerce(caller)

        assert caller == snapshot, "coerce mutated the caller-supplied dict"


class TestNoStateBleeding:
    """Reusing a ScriptModel must not bleed state between uses."""

    def test_reset_gives_clean_state(self):
        script = ScriptModel.from_responses(
            [
                {"type": "text", "content": "a"},
                {"type": "text", "content": "b"},
            ]
        )
        # Consume both entries
        r1, _ = script.next(_payload(), 0)
        r2, _ = script.next(_payload(), 1)
        assert r1.content == "a"
        assert r2.content == "b"
        assert script.exhausted

        # Reset and replay
        script.reset()
        r3, _ = script.next(_payload(), 0)
        r4, _ = script.next(_payload(), 1)
        assert r3.content == "a"
        assert r4.content == "b"

    def test_two_models_from_same_list_are_independent(self):
        shared_responses = [
            {"type": "text", "content": "shared"},
        ]
        model_a = ScriptModel.from_responses(shared_responses)
        model_b = ScriptModel.from_responses(shared_responses)

        # Consume model_a
        model_a.next(_payload(), 0)
        assert model_a.exhausted

        # model_b must be unaffected
        assert not model_b.exhausted
        entry, _ = model_b.next(_payload(), 0)
        assert entry.content == "shared"

    def test_tool_call_arguments_not_shared_across_models(self):
        args = {"key": "value"}
        entry_dict = {"type": "tool_call", "name": "fn", "arguments": args}

        model_a = ScriptModel.from_responses([entry_dict])
        model_b = ScriptModel.from_responses([entry_dict])

        resp_a, _ = model_a.next(_payload(), 0)
        resp_b, _ = model_b.next(_payload(), 0)

        assert isinstance(resp_a, ToolCallResponse)
        assert isinstance(resp_b, ToolCallResponse)

        # Mutating one model's response must not affect the other
        resp_a.arguments["key"] = "mutated"
        assert resp_b.arguments["key"] == "value"

    def test_original_entry_dict_unchanged_after_build(self):
        args = {"original": True}
        entry_dict = {
            "type": "tool_call",
            "name": "fn",
            "arguments": args,
        }
        snapshot_args = dict(args)
        snapshot_entry = copy.deepcopy(entry_dict)

        ScriptModel.from_responses([entry_dict])

        assert args == snapshot_args, "caller's arguments dict was mutated"
        assert entry_dict == snapshot_entry, "caller's entry dict was mutated"

    def test_when_matcher_served_list_independent_across_resets(self):
        script = ScriptModel.from_responses(
            [
                {
                    "type": "text",
                    "content": "conditional",
                    "when": {"prompt_contains": "magic"},
                },
                {"type": "text", "content": "fallback"},
            ]
        )

        # Serve conditional
        r1, _ = script.next(_payload("magic word"), 0)
        assert r1.content == "conditional"

        # Reset clears when-served state
        script.reset()
        r2, _ = script.next(_payload("magic word"), 0)
        assert r2.content == "conditional"
