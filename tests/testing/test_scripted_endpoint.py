# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``lionagi.testing._endpoint.ScriptedEndpoint``."""

from __future__ import annotations

import json

import aiohttp
import pytest

from lionagi.service.connections import match_endpoint
from lionagi.service.imodel import iModel
from lionagi.testing import (
    ScriptedEndpoint,
    ScriptModel,
    TestBranch,
    scripted_imodel,
)


class TestRegistration:
    def test_match_endpoint_returns_scripted(self):
        ep = match_endpoint("scripted", "chat")
        assert isinstance(ep, ScriptedEndpoint)
        assert ep.is_cli is False

    def test_imodel_construction_routes_to_scripted(self):
        script = ScriptModel.from_responses([{"type": "text", "content": "hi"}])
        model = scripted_imodel(script)
        assert isinstance(model.endpoint, ScriptedEndpoint)


class TestEndpointDispatch:
    async def test_text_response_returns_openai_shape(self):
        script = ScriptModel.from_responses([{"type": "text", "content": "hello"}])
        ep = match_endpoint("scripted", "chat", script=script)
        out = await ep._call(payload={"model": "m", "messages": []}, headers={})
        assert out["choices"][0]["message"]["content"] == "hello"
        assert out["choices"][0]["finish_reason"] == "stop"
        assert out["model"] == "m"

    async def test_tool_call_response_includes_tool_calls(self):
        script = ScriptModel.from_responses(
            [
                {
                    "type": "tool_call",
                    "name": "get_weather",
                    "arguments": {"city": "SF"},
                }
            ]
        )
        ep = match_endpoint("scripted", "chat", script=script)
        out = await ep._call(payload={"model": "m", "messages": []}, headers={})
        msg = out["choices"][0]["message"]
        assert msg["content"] is None
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"city": "SF"}
        assert out["choices"][0]["finish_reason"] == "tool_calls"

    async def test_structured_response_serializes_data_as_content(self):
        data = {"sentiment": "positive", "confidence": 0.9}
        script = ScriptModel.from_responses([{"type": "structured", "data": data}])
        ep = match_endpoint("scripted", "chat", script=script)
        out = await ep._call(payload={"model": "m", "messages": []}, headers={})
        assert json.loads(out["choices"][0]["message"]["content"]) == data

    async def test_error_rate_limit_raises_clientresponseerror(self):
        script = ScriptModel.from_responses(
            [{"type": "error", "kind": "rate_limit", "message": "rl"}]
        )
        ep = match_endpoint("scripted", "chat", script=script)
        with pytest.raises(aiohttp.ClientResponseError) as exc:
            await ep._call(payload={"messages": []}, headers={})
        assert exc.value.status == 429
        # Error still recorded for inspection
        assert len(ep.calls) == 1
        assert ep.calls[0].response_type == "error"


class TestRecording:
    async def test_calls_capture_payload_and_match_metadata(self):
        script = ScriptModel.from_responses(
            [
                {"type": "text", "content": "ok"},
                {
                    "type": "text",
                    "content": "matched",
                    "when": {"prompt_contains": "weather"},
                },
            ]
        )
        ep = match_endpoint("scripted", "chat", script=script)

        # First call: positional
        await ep._call(payload={"messages": [{"role": "user", "content": "hi"}]}, headers={})
        # Second call: when:
        await ep._call(
            payload={"messages": [{"role": "user", "content": "weather please"}]},
            headers={},
        )

        assert len(ep.calls) == 2
        first, second = ep.calls
        assert first.matched_by == "positional"
        assert first.last_user_message == "hi"
        assert "prompt_contains" in second.matched_by
        assert second.last_user_message == "weather please"

    async def test_clear_calls_resets_recording(self):
        ep = match_endpoint(
            "scripted",
            "chat",
            script=ScriptModel.from_responses(
                [{"type": "text", "content": "a"}, {"type": "text", "content": "b"}]
            ),
        )
        await ep._call(payload={"messages": []}, headers={})
        ep.clear_calls()
        assert ep.calls == []


class TestBranchIntegration:
    async def test_text_response_via_chat(self):
        branch = TestBranch.from_text("hello back")
        result = await branch.chat("hi")
        assert result == "hello back"

    async def test_sequence_of_responses(self):
        branch = TestBranch.from_text(["one", "two", "three"])
        r1 = await branch.chat("a")
        r2 = await branch.chat("b")
        r3 = await branch.chat("c")
        assert (r1, r2, r3) == ("one", "two", "three")

    async def test_calls_inspectable_via_helper(self):
        branch = TestBranch.from_text("ok")
        await branch.chat("the question")
        calls = TestBranch.calls(branch)
        assert len(calls) == 1
        # branch.chat wraps the user text with an "Instruction:" prefix;
        # asserting on `in` is the right granularity for tests.
        assert "the question" in calls[0].last_user_message

    def test_scripted_helper_rejects_non_scripted_branch(self):
        # Build a vanilla iModel without scripted provider
        plain = iModel(provider="openai", model="gpt-4.1-mini", api_key="x")
        from lionagi.session.branch import Branch

        branch = Branch(chat_model=plain)
        with pytest.raises(TypeError, match="not ScriptedEndpoint"):
            TestBranch.scripted(branch)
