# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""OpenAI chat prompt-cache placement and option-off compatibility."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from lionagi.operations.chat._prepare import _prepare_run_kwargs
from lionagi.operations.types import ChatParam
from lionagi.protocols.messages import ActionRequest, ActionResponse
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch

FIXTURES = Path(__file__).parent / "fixtures" / "openai_prompt_cache_off.json"
SYSTEM = "Review the evidence carefully."


def lookup_order(order_id: str) -> str:
    """Find an order by its id."""
    return f"FOUND {order_id}"


def _model(model, caching, filtered):
    result = iModel(
        provider="openai", model=model, api_key="test-not-a-key", prompt_caching=caching
    )
    if not filtered:
        result.endpoint.request_options = None
    return result


async def _payloads(model, caching, filtered):
    imodel = _model(model, caching, filtered)
    branch = Branch(system=SYSTEM, chat_model=imodel, tools=[lookup_order])
    payloads = []
    for index, prompt in enumerate(("Find order A1", "Check the result", "Summarize it")):
        param = ChatParam(tool_schemas=branch.acts.schema_list) if index == 0 else ChatParam()
        ins, request = _prepare_run_kwargs(branch, prompt, param, imodel=imodel)
        payload, _ = imodel.endpoint.create_payload(request)
        payloads.append(payload)
        branch.msgs.add_message(instruction=ins)
        branch.msgs.add_message(assistant_response=f"Answer {index + 1}")
        if index == 1:
            result = ActionResponse(
                content={
                    "function": "lookup_order",
                    "arguments": {"order_id": "A1"},
                    "output": "FOUND",
                }
            )
            await branch.msgs._a_add_built_response(result)
    return payloads


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-5", "gpt-6-luna"])
@pytest.mark.parametrize("filtered", [True, False])
async def test_cache_off_matches_pre_change_bytes(model, filtered):
    payloads = await _payloads(model, False, filtered)
    key = f"{model}-{'filtered' if filtered else 'unfiltered'}"
    fixture = json.loads(FIXTURES.read_text())
    assert _bytes(payloads) == fixture[key].encode()


@pytest.mark.parametrize("model", ["gpt-5", "gpt-6-luna"])
@pytest.mark.parametrize("filtered", [True, False])
def test_direct_cache_off_matches_pre_change_bytes(model, filtered):
    endpoint = _model(model, False, filtered).endpoint
    request = {
        "model": model,
        "messages": [
            {"role": "system", "name": "policy", "content": "existing"},
            {"role": "user", "content": "hello"},
        ],
        "system_blocks": ["unused request"],
        "vendor_extension": "preserve-only-if-unfiltered",
    }
    original = copy.deepcopy(request)
    payload, _ = endpoint.create_payload(request, system_blocks=["unused kwarg"])
    assert request == original
    key = f"{model}-{'filtered' if filtered else 'unfiltered'}"
    fixture = json.loads((FIXTURES.parent / "openai_direct_cache_off.json").read_text())
    assert _bytes(payload) == fixture[key].encode()


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "role"), [("gpt-5", "developer"), ("gpt-6-luna", "system")])
@pytest.mark.parametrize("filtered", [True, False])
async def test_cache_on_keeps_the_conversation_prefix(model, role, filtered):
    payloads = await _payloads(model, True, filtered)
    for previous, current in zip(payloads, payloads[1:], strict=False):
        before, after = previous["messages"], current["messages"]
        assert len(after) > len(before)
        assert _bytes(after[: len(before)]) == _bytes(before)
    for payload in payloads:
        messages = payload["messages"]
        assert messages[0]["role"] == role
        assert SYSTEM in str(messages[0]["content"])
        assert "Find an order by its id." in str(messages[0]["content"])
        assert sum("Find an order by its id." in str(message) for message in messages) == 1
        assert all(SYSTEM not in str(message) for message in messages if message["role"] == "user")
        assert "prompt_caching" not in payload
        assert "system_blocks" not in payload


@pytest.mark.parametrize(("model", "role"), [("gpt-5", "developer"), ("gpt-6-luna", "system")])
@pytest.mark.parametrize("filtered", [True, False])
def test_system_blocks_merge_once_without_mutating_request(model, role, filtered):
    endpoint = _model(model, True, filtered).endpoint
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": "existing", "name": "policy"},
            {"role": "user", "content": "go"},
        ],
        "system_blocks": ["ignored"],
    }
    original = copy.deepcopy(request)
    stable = ["stable", "tools"]
    payload, _ = endpoint.create_payload(request, system_blocks=stable)
    assert request == original
    assert stable == ["stable", "tools"]
    assert [message["role"] for message in payload["messages"]] == [role, "user"]
    assert payload["messages"][0]["name"] == "policy"
    content = str(payload["messages"][0]["content"])
    assert content.index("existing") < content.index("stable") < content.index("tools")
    assert "ignored" not in content


def test_stable_blocks_appear_once_in_the_system_message():
    endpoint = _model("gpt-6-luna", True, True).endpoint
    request = {
        "model": "gpt-6-luna",
        "messages": [{"role": "user", "content": "go"}],
        "system_blocks": ["stable policy", "stable tools"],
    }
    payload, _ = endpoint.create_payload(request)
    systems = [m for m in payload["messages"] if m["role"] == "system"]
    assert len(systems) == 1
    assert str(systems[0]["content"]).count("stable policy") == 1
    assert str(systems[0]["content"]).count("stable tools") == 1


@pytest.mark.parametrize("filtered", [True, False])
def test_cache_placement_rejects_non_text_system_blocks(filtered):
    endpoint = _model("gpt-6-luna", True, filtered).endpoint
    with pytest.raises(ValueError, match="system blocks must be text"):
        endpoint.create_payload(
            {
                "messages": [{"role": "user", "content": "go"}],
                "system_blocks": [
                    {"type": "image_url", "image_url": {"url": "https://example.invalid/a"}}
                ],
            }
        )


@pytest.mark.parametrize("filtered", [True, False])
@pytest.mark.parametrize(
    "block",
    [
        {"type": "text", "text": "safe", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "safe", "private_marker": "private"},
        {"type": "text", "text": 123},
        {"type": "text"},
    ],
)
def test_cache_placement_refuses_invalid_text_blocks(filtered, block):
    endpoint = _model("gpt-6-luna", True, filtered).endpoint
    with pytest.raises(ValueError, match="OpenAI system blocks must be text"):
        endpoint.create_payload(
            {"messages": [{"role": "user", "content": "go"}], "system_blocks": [block]}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "role"), [("gpt-5", "developer"), ("gpt-6-luna", "system")])
async def test_branch_chat_reuses_prefix_across_three_rounds(model, role):
    imodel = _model(model, True, True)
    branch = Branch(system=SYSTEM, chat_model=imodel, tools=[lookup_order])

    class FirstTurnContext:
        calls = 0

        async def provide(self, branch, instruction):
            self.calls += 1
            return "FIRST-TURN EVIDENCE" if self.calls == 1 else None

    branch.providers.register(FirstTurnContext())
    sent = []
    build = imodel.endpoint.create_payload

    def capture(*args, **kwargs):
        payload, headers = build(*args, **kwargs)
        sent.append(copy.deepcopy(payload))
        return payload, headers

    async def call(*args, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "An answer."}}]}

    with (
        patch.object(imodel.endpoint, "create_payload", side_effect=capture),
        patch.object(imodel.endpoint, "call", side_effect=call),
    ):
        await branch.chat_and_record("First question")
        await branch.chat_and_record("Second question")
        action = ActionRequest(
            content={"function": "lookup_order", "arguments": {"order_id": "A1"}}
        )
        result = await branch.act(action)
        assert result[0].output == "FOUND A1"
        await branch.chat_and_record("Third question")
        await branch.chat_and_record("Fourth question")

    assert len(sent) == 4
    assert [message["role"] for message in sent[2]["messages"]] == [
        role,
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert "FOUND" in str(sent[2]["messages"][-1])
    assert all("FIRST-TURN EVIDENCE" in str(payload["messages"]) for payload in sent)
    for previous, current in zip(sent, sent[1:], strict=False):
        before, after = previous["messages"], current["messages"]
        assert len(after) > len(before)
        assert _bytes(after[: len(before)]) == _bytes(before)
    assert all("Find an order by its id." in str(payload["messages"][0]) for payload in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("caching", [True, False])
async def test_token_usage_estimate_preserves_cached_conversation_prefix(caching):
    imodel = _model("gpt-6-luna", caching, True)
    branch = Branch(system=SYSTEM, chat_model=imodel)
    sent = []

    async def call(*args, **kwargs):
        sent.append(copy.deepcopy(kwargs["request"]))
        return {"choices": [{"message": {"role": "assistant", "content": "An answer."}}]}

    with patch.object(imodel.endpoint, "call", side_effect=call):
        await branch.chat_and_record("First question", include_token_usage_to_model=True)
        await branch.chat_and_record("Second question", include_token_usage_to_model=True)

    first_user = sent[0]["messages"][-1]
    history_user = next(m for m in sent[1]["messages"] if m["role"] == "user")
    marker = "Estimated Current Token Usage:"
    if caching:
        assert _bytes(first_user) == _bytes(history_user)
        assert marker not in str(first_user)
        assert marker not in str(history_user)
    else:
        assert marker in str(first_user)
        assert marker in str(sent[1]["messages"][-1])
        assert marker not in str(history_user)
