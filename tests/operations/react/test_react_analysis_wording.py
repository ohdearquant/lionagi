# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The round's response schema never asks the model for its chain of thought.

Every ReAct round sends the ``ReActAnalysis`` schema, so a deep run carries one copy of the
``analysis`` field description per round. Worded as a request for a chain-of-thought summary,
those copies made Claude Sonnet 5.5 refuse mid-run (``stop_reason`` ``refusal``, category
``reasoning_extraction``); replaying the refused requests with only that sentence reworded got
an answer every time, while rewording an unrelated sentence of the same schema did not.
"""

import json

import pytest

from lionagi.operations.ReAct.utils import ReActAnalysis
from lionagi.testing import TestBranch

ANALYSIS_DESCRIPTION_START = "Free-form notes on progress so far."
FORBIDDEN = ("chain-of-thought", "chain of thought")


def test_the_analysis_field_description_does_not_ask_for_a_chain_of_thought():
    description = ReActAnalysis.model_json_schema()["properties"]["analysis"]["description"]
    assert description.startswith(ANALYSIS_DESCRIPTION_START)
    assert not any(phrase in description.lower() for phrase in FORBIDDEN)


@pytest.mark.asyncio
async def test_no_round_request_asks_the_model_for_a_chain_of_thought():
    """Every request of a two-round run carries the schema and none of its wording asks for one."""

    def echo(text: str) -> str:
        """Return the text it was given."""
        return text

    responses = [
        json.dumps(
            {
                "analysis": "Call the tool once, then answer.",
                "extension_needed": True,
                "action_requests": [{"function": "echo", "arguments": {"text": "one"}}],
            }
        ),
        json.dumps({"analysis": "The tool answered.", "extension_needed": False}),
        json.dumps({"answer": "done"}),
    ]
    branch = TestBranch.from_text(responses, tools=[echo])

    assert await branch.ReAct(instruction="echo once", tools=True, max_extensions=1) == "done"

    calls = TestBranch.calls(branch)
    round_payloads = [
        json.dumps(call.payload, ensure_ascii=False)
        for call in calls
        if ANALYSIS_DESCRIPTION_START in json.dumps(call.payload, ensure_ascii=False)
    ]
    # The schema rides on the reason-act rounds; without them the absence below proves nothing.
    assert len(round_payloads) >= 2
    for payload in (json.dumps(call.payload, ensure_ascii=False) for call in calls):
        assert not any(phrase in payload.lower() for phrase in FORBIDDEN)
