# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""With the prompt cache off an `all` fold keeps the system message and the task ahead of its note
and folds every later instruction in its span, as before ADR-0130 Amendment 4."""

import pytest

from lionagi.agent.context_fold import ContextFold
from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from tests.cli.test_agent_api_provider import _text

_CAP = 8_000


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_tail", [2, 4, 6])
async def test_cache_off_all_fold_keeps_the_task_and_folds_later_instructions(
    monkeypatch, keep_tail
):
    async def call(self, request, *args, **kwargs):
        return _text("SUMMARY: src/app/core.py checked")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    model = iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        max_input_tokens=_CAP,
        prompt_caching=False,
    )
    branch = Branch(chat_model=model, system="You check source files.")
    for turn in range(4):
        await branch.msgs.a_add_message(instruction=f"turn {turn}: check src/app/core.py")
        await branch.msgs.a_add_message(assistant_response=f"turn {turn} " + "detail " * 600)
    before = list(branch.progression)
    fold = ContextFold(branch, cap=_CAP)
    branch.metadata["context_delivered"] = [str(uid) for uid in before]

    assert await fold._fold_once("all", keep_tail, "threshold")

    active = list(branch.progression)
    end = len(before) - keep_tail
    assert active[:2] == before[:2], "the system message and the task stay ahead of the note"
    assert all(uid not in active for uid in before[2:end]), "later instructions fold"
    assert active[3:] == before[end:], "the kept tail is untouched"
    note = branch.msgs.messages[active[2]]
    assert "context_first_index" in note.metadata
    assert f"The host folded {end - 2} earlier messages" in str(note.content.assistant_response)
