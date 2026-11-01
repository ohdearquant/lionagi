# SPDX-License-Identifier: Apache-2.0

"""An API agent turn whose first response ran away at its output limit with text.

A response can stop at ``max_tokens`` after one sentence and a very long run of one repeated
string, with no tool call. The turn must still end with an answer. The provider is scripted and
request tokens are counted offline, so nothing leaves the process, and the input cap is small so
the run is fast.

The remaining tests take the context fold alone: a view that is over the cap only because of that
one text must be foldable back under the cap, whether the text is one of the last two messages or
not, the text is clipped as a copy that records the message it came from, and a long text whose
response ended normally is never clipped.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.cli import test_agent_api_context_fold as _fold_tests
from tests.cli import test_agent_api_provider as _provider_tests
from tests.cli.test_agent_api_provider import (
    _answer,
    _cut_off,
    _last_round,
    _last_user_text,
    _profile,
    _text,
)

leg = _provider_tests.leg  # the scripted-endpoint fixture the provider tests run on


class _OfflineEncoding(_provider_tests._OfflineEncoding):
    """The provider tests' offline encoding plus ``decode``, which a fold's token count looks up."""

    def decode(self, tokens, **_kwargs):
        return "x" * (4 * len(tokens))


@pytest.fixture
def offline_tokens(monkeypatch):
    """Count request and fold tokens without fetching the encoding file from the network."""
    import tiktoken

    monkeypatch.setattr(tiktoken, "get_encoding", lambda _name: _OfflineEncoding())


_CAP = 8_000
_SENTENCE = "I will start by reading the notes file."
# Far more tokens than _CAP, whatever the tokenizer makes of the repeated string.
_REPEATS = 20_000
_LONG_TEXT = _SENTENCE + "<br>" * _REPEATS


def _runaway() -> dict:
    """A response cut off at its output limit: one sentence, then the same string over and over."""
    return _cut_off({"type": "text", "text": _LONG_TEXT})


def _ended_normally() -> dict:
    """The same long text, in a response that ended its turn (``stop_reason`` ``end_turn``)."""
    return _provider_tests._text(_LONG_TEXT)


def _branch_with_long_text(monkeypatch, response: dict, *, short_after: int = 0):
    """A branch whose view is the task, one assistant message recording ``response`` as its
    provider response, then ``short_after`` short messages. Returns the branch and that message.

    The summary request a fold makes is answered in-process."""
    from lionagi import Branch
    from lionagi.protocols.messages import AssistantResponse

    async def summary_reply(self, request, *args, **kwargs):
        return _fold_tests._text("SUMMARY: the notes file was read")

    monkeypatch.setattr(_fold_tests.AnthropicMessagesEndpoint, "call", summary_reply)
    branch = Branch(chat_model=_fold_tests._haiku(_CAP))
    msgs = branch.msgs
    long_message = AssistantResponse.from_response(response)
    view = [msgs.create_instruction(instruction="read the notes file and report"), long_message]
    for number in range(short_after):
        view.append(
            msgs.create_instruction(instruction="continue")
            if number % 2 == 0
            else msgs.create_assistant_response(assistant_response="ok")
        )
    for message in view:
        msgs.messages.include(message)
        msgs.progression.include(message.id)
    return branch, long_message


def _clipped_assistant_copies(branch) -> list:
    from lionagi.protocols.messages import AssistantResponse

    return [
        message
        for message in branch.msgs.messages
        if isinstance(message, AssistantResponse) and message.metadata.get("clipped_from")
    ]


@pytest.mark.usefixtures("offline_tokens")
@pytest.mark.asyncio
async def test_a_turn_whose_first_response_ran_away_at_its_output_limit_still_answers(leg):
    # The runaway response is cut off, so it is not sent to a repair request, which would send
    # all of it again: its reply stays unparsed. The answer turn follows, then the second pass
    # that the unparsed reply continues: a reply that ends its loop. That pass makes a context
    # fold's summary request before its answer turn, which the fourth reply answers.
    leg.replies = [
        _runaway(),
        _answer("read them"),
        _last_round(),
        _text("SUMMARY: the notes file was read"),
        _answer("the notes say hello"),
    ]

    result, _provider, _branch_id, status, _summaries = await leg.run(
        _profile(api_tools="read", max_input_tokens=_CAP)
    )

    assert (result, status) == ("the notes say hello", "completed")
    assert leg.replies == []
    assert len(leg.payloads) == 5
    assert not any("text_to_format" in json.dumps(p, default=str) for p in leg.payloads)
    resumed = _last_user_text(leg.payloads[2])
    assert "could not be read in the required response format" in resumed
    assert "stopped at its output token limit" in resumed
    assert "no JSON object could be read from it" not in resumed


@pytest.mark.usefixtures("offline_tokens")
@pytest.mark.asyncio
async def test_a_view_over_the_cap_because_of_one_runaway_text_can_be_folded(monkeypatch):
    from lionagi.agent.context_fold import ContextFold

    branch, _message = _branch_with_long_text(monkeypatch, _runaway())
    fold = ContextFold(branch, cap=_CAP)
    over = fold.estimate()
    assert over > _CAP

    landed = await fold.fold(reason="refused", observed=over)

    assert (landed, fold.estimate() <= _CAP) == (True, True)


@pytest.mark.usefixtures("offline_tokens")
@pytest.mark.asyncio
async def test_a_runaway_text_followed_by_two_short_messages_can_be_folded(monkeypatch):
    from lionagi.agent.context_fold import ContextFold

    branch, _message = _branch_with_long_text(monkeypatch, _runaway(), short_after=2)
    fold = ContextFold(branch, cap=_CAP)
    over = fold.estimate()
    assert over > _CAP

    landed = await fold.fold(reason="refused", observed=over)

    assert (landed, fold.estimate() <= _CAP) == (True, True)


@pytest.mark.usefixtures("offline_tokens")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("short_after", "landed_expected", "still_in_view"),
    [
        # One of the last two messages: nothing in the view can be folded, so nothing lands.
        (0, False, True),
        # Followed by two short messages: the summary fold takes it, as it always has.
        (2, True, False),
    ],
)
async def test_a_long_text_whose_response_ended_normally_is_never_clipped(
    monkeypatch, short_after, landed_expected, still_in_view
):
    from lionagi.agent.context_fold import ContextFold

    branch, message = _branch_with_long_text(
        monkeypatch, _ended_normally(), short_after=short_after
    )
    fold = ContextFold(branch, cap=_CAP)
    over = fold.estimate()
    assert over > _CAP

    landed = await fold.fold(reason="refused", observed=over)

    assert (landed, message.id in branch.progression, _clipped_assistant_copies(branch)) == (
        landed_expected,
        still_in_view,
        [],
    )


@pytest.mark.usefixtures("offline_tokens")
@pytest.mark.asyncio
async def test_a_clipped_runaway_text_is_a_copy_that_records_the_original(monkeypatch):
    from lionagi.agent.context_fold import ContextFold

    branch, original = _branch_with_long_text(monkeypatch, _runaway())
    fold = ContextFold(branch, cap=_CAP)

    await fold.fold(reason="refused", observed=fold.estimate())

    (copy,) = _clipped_assistant_copies(branch)
    assert copy.id in branch.progression
    assert original.id not in branch.progression
    assert copy.metadata["clipped_from"] == str(original.id)
    # The original is untouched in the pile, text and recorded response included.
    assert branch.msgs.messages[original.id] is original
    assert original.response == _LONG_TEXT
    assert original.model_response["stop_reason"] == "max_tokens"
    # The copy keeps the beginning of the text and says how many characters it cut.
    cut = re.search(r"\n\[(\d+) characters cut", copy.response)
    assert cut is not None
    kept = copy.response[: cut.start()]
    assert _LONG_TEXT.startswith(kept)
    assert kept.startswith(_SENTENCE)
    assert len(_LONG_TEXT) - len(kept) == int(cut.group(1))
