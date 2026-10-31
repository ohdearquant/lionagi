# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The cache intent each operation declares for the requests it makes (ADR-0127 Amendment 3).

A request after which nothing of the conversation follows is ``final``: it reads what earlier
requests cached and writes no entry of its own.
"""

import json

import pytest
from pydantic import BaseModel

from lionhub.lndl import LNDLError
from lionhub.lndl.middle.lndl_middle import build_lndl_middle
from lionagi.operations.types import ChatParam
from lionagi.service import _prompt_cache as ledger_mod
from lionagi.testing import TestBranch

MISSING = "```lndl\nOUT{answer: [missing]}\n```"
ANSWER = "```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```"


class _Answer(BaseModel):
    answer: str


def _watch(branch, monkeypatch):
    """The intent in force as each request is built, on the chat path and the streamed path."""
    seen = []
    model = branch.chat_model
    create = model.create_event

    async def record(*args, **kwargs):
        seen.append(ledger_mod.current_intent())
        return await create(*args, **kwargs)

    monkeypatch.setattr(model, "create_event", record)
    return seen


@pytest.mark.asyncio
async def test_react_asks_for_its_answer_under_the_final_intent(monkeypatch):
    def multiply(a: float, b: float) -> float:
        """Multiply two numbers."""
        return a * b

    request = {"function": "multiply", "arguments": {"a": 6, "b": 7}}
    branch = TestBranch.from_text(
        [
            json.dumps(
                {"analysis": "Multiply", "extension_needed": False, "action_requests": [request]}
            ),
            json.dumps({"answer": "42"}),
        ],
        tools=[multiply],
        log_config={"auto_save_on_exit": False},
    )
    intents = _watch(branch, monkeypatch)

    result = await branch.ReAct(
        instruction="What is 6 times 7?", tools=[multiply], max_extensions=0
    )

    assert result == "42"
    assert intents == ["continuing", "final"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget, replies, expected",
    [
        (2, [ANSWER], ["continuing"]),
        (2, [MISSING, ANSWER], ["continuing", "final"]),
        (1, [ANSWER], ["final"]),
    ],
    ids=["answered early", "answered in the last round", "one round"],
)
async def test_lndl_makes_only_its_last_round_under_the_final_intent(
    monkeypatch, budget, replies, expected
):
    branch = TestBranch.from_text(replies)
    intents = _watch(branch, monkeypatch)
    result = await build_lndl_middle(round_budget=budget)(
        branch, "say hi", ChatParam(response_format=_Answer)
    )

    assert result.answer == "hi"
    assert intents == expected


@pytest.mark.asyncio
async def test_an_exhausted_lndl_run_made_its_last_round_under_the_final_intent(monkeypatch):
    branch = TestBranch.from_text([MISSING, MISSING])
    intents = _watch(branch, monkeypatch)

    with pytest.raises(LNDLError):
        await build_lndl_middle(round_budget=2)(
            branch, "say hi", ChatParam(response_format=_Answer)
        )

    assert intents == ["continuing", "final"]


@pytest.mark.asyncio
async def test_a_parse_repair_is_made_under_the_final_intent(monkeypatch):
    branch = TestBranch.from_text(['{"answer": "fixed"}'])
    intents = _watch(branch, monkeypatch)

    result = await branch.parse("no structure here", response_format=_Answer, max_retries=1)

    assert result.answer == "fixed"
    assert intents == ["final"]


@pytest.mark.asyncio
async def test_a_request_outside_any_operation_is_continuing(monkeypatch):
    branch = TestBranch.from_text(["hello"])
    intents = _watch(branch, monkeypatch)

    await branch.chat("hi")

    assert intents == ["continuing"]
