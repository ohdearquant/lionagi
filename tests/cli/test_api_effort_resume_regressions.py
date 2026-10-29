# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A resumed API request respects the effort policy of its new model."""

import json

import pytest

from tests.cli.test_agent_api_provider import _profile

pytest_plugins = ("tests.cli.test_agent_api_provider",)


@pytest.mark.asyncio
async def test_profile_effort_drop_removes_persisted_effort_from_resumed_payload(leg, monkeypatch):
    from lionagi import Branch
    from lionagi.cli._runs import find_branch
    from lionagi.providers.openai.chat import OpenaiChatEndpoint

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    profile = _profile(api_tools="read")
    profile.effort = "high"
    replies = []
    payloads = []

    async def call(self, request, *args, **kwargs):
        payloads.append(request)
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": request["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(replies.pop(0))},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def add_replies(answer):
        replies.extend(
            [
                {
                    "analysis": "done",
                    "action_requests": [],
                    "action_strategy": "sequential",
                    "extension_needed": False,
                    "reason": {"title": "done", "content": "nothing left"},
                },
                {"answer": answer},
            ]
        )

    monkeypatch.setattr(OpenaiChatEndpoint, "call", call)
    add_replies("first")
    result, _, branch_id, status, _ = await leg.run(profile, model="openai/gpt-6-luna")
    assert (result, status) == ("first", "completed")
    assert payloads and all(p["reasoning_effort"] == "high" for p in payloads)

    _, branch_path = find_branch(branch_id)
    stored = Branch.from_dict(json.loads(branch_path.read_text()))
    assert stored.chat_model.endpoint.config.kwargs["reasoning_effort"] == "high"

    payloads.clear()
    add_replies("second")
    result, _, resumed_id, status, _ = await leg.run(
        profile, "continue", model="openai/gpt-4.1", resume=branch_id
    )
    assert (result, resumed_id, status) == ("second", branch_id, "completed")
    assert payloads and all(p["model"] == "gpt-4.1" for p in payloads)
    assert all("reasoning_effort" not in p for p in payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new_model", "expected", "same_model"),
    [
        ("claude-sonnet-4-6", "high", False),
        ("claude-haiku-5-5", "xhigh", False),
        ("claude-sonnet-4-6", "high", True),
    ],
)
async def test_anthropic_resume_reclamps_persisted_effort(leg, new_model, expected, same_model):
    from lionagi import Branch
    from lionagi.cli._runs import find_branch
    from tests.cli.test_agent_api_provider import _answer, _last_round

    profile = _profile(api_tools="read")
    profile.effort = "xhigh"
    leg.replies = [_last_round(), _answer("first")]
    result, _, branch_id, status, _ = await leg.run(profile, model="anthropic/claude-opus-5-5")
    assert (result, status) == ("first", "completed")
    assert leg.payloads and all(p["output_config"]["effort"] == "xhigh" for p in leg.payloads)
    _, branch_path = find_branch(branch_id)
    stored = Branch.from_dict(json.loads(branch_path.read_text()))
    assert stored.chat_model.endpoint.config.kwargs["output_config"]["effort"] == "xhigh"
    if same_model:
        stored.chat_model.endpoint.config.kwargs["model"] = new_model
        branch_path.write_text(json.dumps(stored.to_dict()))

    leg.payloads.clear()
    leg.replies = [_last_round(), _answer("second")]
    result, _, resumed_id, status, _ = await leg.run(
        _profile(api_tools="read"),
        "continue",
        model=None if same_model else f"anthropic/{new_model}",
        resume=branch_id,
    )
    assert (result, resumed_id, status) == ("second", branch_id, "completed")
    assert leg.payloads and all(p["model"] == new_model for p in leg.payloads)
    assert all(p["output_config"]["effort"] == expected for p in leg.payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("explicit", "same_model"),
    [(False, False), (True, False), (False, True)],
    ids=["model-change", "explicit", "legacy-same-model"],
)
async def test_openai_resume_on_non_reasoning_model_handles_stored_effort(
    leg, monkeypatch, explicit, same_model
):
    from lionagi import Branch
    from lionagi._errors import ConfigurationError
    from lionagi.cli._runs import find_branch
    from lionagi.providers.openai.chat import OpenaiChatEndpoint

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    payloads = []
    replies = []

    async def call(self, request, *args, **kwargs):
        payloads.append(request)
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": request["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(replies.pop(0))},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def add_replies(answer):
        replies.extend(
            [
                {
                    "analysis": "done",
                    "action_requests": [],
                    "action_strategy": "sequential",
                    "extension_needed": False,
                    "reason": {"title": "done", "content": "nothing left"},
                },
                {"answer": answer},
            ]
        )

    monkeypatch.setattr(OpenaiChatEndpoint, "call", call)
    profile = _profile(api_tools="read")
    profile.effort = "high"
    add_replies("first")
    _, _, branch_id, status, _ = await leg.run(profile, model="openai/gpt-6-luna")
    assert status == "completed"
    assert payloads and all(p["reasoning_effort"] == "high" for p in payloads)
    _, branch_path = find_branch(branch_id)
    stored = Branch.from_dict(json.loads(branch_path.read_text()))
    assert stored.chat_model.endpoint.config.kwargs["reasoning_effort"] == "high"
    if same_model:
        stored.chat_model.endpoint.config.kwargs["model"] = "gpt-4.1"
        branch_path.write_text(json.dumps(stored.to_dict()))

    payloads.clear()
    resume_kwargs = {"effort": "high"} if explicit else {}
    if explicit:
        with pytest.raises(ConfigurationError, match="openai.*gpt-4.1.*high"):
            await leg.run(
                _profile(api_tools="read"),
                "continue",
                model="openai/gpt-4.1",
                resume=branch_id,
                **resume_kwargs,
            )
        assert payloads == []
    else:
        add_replies("second")
        result, _, resumed_id, status, _ = await leg.run(
            _profile(api_tools="read"),
            "continue",
            model=None if same_model else "openai/gpt-4.1",
            resume=branch_id,
            **resume_kwargs,
        )
        assert (result, resumed_id, status) == ("second", branch_id, "completed")
        assert payloads and all(p["model"] == "gpt-4.1" for p in payloads)
        assert all("reasoning_effort" not in p for p in payloads)
        _, updated_path = find_branch(branch_id)
        updated = Branch.from_dict(json.loads(updated_path.read_text()))
        assert "reasoning_effort" not in updated.chat_model.endpoint.config.kwargs
