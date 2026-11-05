# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A failed parse has two very different causes, and they need different fixes.

Either no JSON could be recovered from the text, or JSON was recovered intact
and the response model refused it. These tests pin that the fenced-block
extraction path works, that the two causes stay distinguishable, and that the
reformat retries do not reissue an identical request.
"""

import copy
import importlib
import json
import pickle
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from lionagi.operations._defaults import make_parse_param
from lionagi.operations.communicate.communicate import (
    communicate,
    prepare_communicate_kw,
)
from lionagi.operations.parse import (
    ExtractionError,
    SchemaRejectedError,
    UnparsedResponse,
)
from lionagi.operations.parse.parse import _validate_dict_or_model
from lionagi.operations.parse.parse import parse as _parse
from lionagi.operations.types import ParseParam
from lionagi.protocols.types import AssistantResponse
from lionagi.session.branch import Branch
from lionagi.testing import LionAGIMockFactory


class Course(BaseModel):
    name: str
    credits: int


class Transcript(BaseModel):
    student: str
    courses: list[Course]


# A short preamble line, then a complete fenced JSON block. Row 2 carries a
# null where the schema requires a string.
FENCED_SCHEMA_VIOLATION = """Here is the extracted transcript from the image:

```json
{
  "student": "A. Rivera",
  "courses": [
    {"name": "Linear Algebra", "credits": 4},
    {"name": "Organic Chemistry", "credits": 5},
    {"name": null, "credits": 3}
  ]
}
```"""

FENCED_CONFORMANT = FENCED_SCHEMA_VIOLATION.replace('"name": null', '"name": "Thermodynamics"')

NOTHING_PARSEABLE = "I'm sorry, I can't read that image clearly enough to extract a transcript."


def _fmp():
    return make_parse_param(Transcript, None).fuzzy_match_params


async def _parse_returning(body: str, **kw):
    """Run the real parse loop with the reformat turn stubbed out."""
    branch = Branch()

    async def fake_chat(*_a, **_kw):
        return None, AssistantResponse(
            role="assistant",
            content={"assistant_response": body},
            sender=branch.id,
            recipient=branch.user,
        )

    param = make_parse_param(Transcript, None, **kw)
    with patch.object(Branch, "chat", fake_chat):
        return await _parse(branch, body, param)


class TestFencedExtraction:
    """The fenced block itself extracts fine; only the schema rejects it."""

    def test_preamble_plus_fenced_block_validates_when_conformant(self):
        result = _validate_dict_or_model(FENCED_CONFORMANT, Transcript, _fmp())

        assert isinstance(result, Transcript)
        assert [c.name for c in result.courses] == [
            "Linear Algebra",
            "Organic Chemistry",
            "Thermodynamics",
        ]

    def test_schema_violation_reaches_validation_not_extraction(self):
        """The JSON is recovered whole; pydantic is what refuses it."""
        with pytest.raises(SchemaRejectedError) as exc_info:
            _validate_dict_or_model(FENCED_SCHEMA_VIOLATION, Transcript, _fmp())

        underlying = exc_info.value.validation_error
        assert underlying is not None
        first = underlying.errors()[0]
        assert tuple(first["loc"]) == ("courses", 2, "name")
        assert first["type"] == "string_type"
        assert first["input"] is None


class TestFailureKindSurvives:
    """A caller must be able to tell the two causes apart after the retries."""

    @pytest.mark.asyncio
    async def test_schema_rejection_is_reported_as_validation(self):
        result = await _parse_returning(FENCED_SCHEMA_VIOLATION)

        assert isinstance(result, UnparsedResponse)
        assert result.failure_kind == "validation"
        assert result.validation_error is not None
        first = result.validation_error.errors()[0]
        assert tuple(first["loc"]) == ("courses", 2, "name")

    @pytest.mark.asyncio
    async def test_unrecoverable_text_is_reported_as_extraction(self):
        result = await _parse_returning(NOTHING_PARSEABLE)

        assert isinstance(result, UnparsedResponse)
        assert result.failure_kind == "extraction"
        assert result.validation_error is None

    @pytest.mark.asyncio
    async def test_raw_text_return_stays_a_plain_string(self):
        """The degradation is deliberate; attaching a reason must not break it."""
        result = await _parse_returning(FENCED_SCHEMA_VIOLATION)

        assert isinstance(result, str)
        assert result == FENCED_SCHEMA_VIOLATION
        assert f"{result}" == FENCED_SCHEMA_VIOLATION

    @pytest.mark.asyncio
    async def test_raise_mode_carries_the_validation_error(self):
        with pytest.raises(ValueError) as exc_info:
            await _parse_returning(FENCED_SCHEMA_VIOLATION, handle_validation="raise")

        assert isinstance(exc_info.value, SchemaRejectedError)
        assert exc_info.value.validation_error is not None


class TestReformatRequestsVary:
    """An identical request on a deterministic engine cannot change the outcome."""

    @pytest.mark.asyncio
    async def test_each_reformat_turn_sends_different_guidance(self):
        branch = Branch()
        seen: list[str] = []

        async def fake_chat(*_a, **kw):
            seen.append(kw.get("guidance"))
            return None, AssistantResponse(
                role="assistant",
                content={"assistant_response": FENCED_SCHEMA_VIOLATION},
                sender=branch.id,
                recipient=branch.user,
            )

        param = make_parse_param(Transcript, None, num_retries=3)
        with patch.object(Branch, "chat", fake_chat):
            await _parse(branch, FENCED_SCHEMA_VIOLATION, param)

        assert len(seen) == 4, "expected 1 attempt plus 3 retries"
        assert len(set(seen)) == len(seen), "retries reissued an identical request"
        # The failure that preceded each turn is what makes it differ.
        assert all("courses.2.name" in g for g in seen)


class TestOperateSurfacesTheDistinction:
    """operate() is the entry point this was reported through.

    It pins parse to ``return_value`` internally, so whatever parse degrades to
    is what a caller of operate() actually receives.
    """

    @staticmethod
    def _branch(body: str):
        return LionAGIMockFactory.create_mocked_branch(response=body, model="gpt-4.1-mini")

    async def _operate(self, body: str, **kw):
        return await self._branch(body).operate(
            instruction="extract the transcript",
            response_format=Transcript,
            invoke_actions=False,
            **kw,
        )

    @pytest.mark.asyncio
    async def test_default_return_carries_the_reason_through_operate(self):
        result = await self._operate(FENCED_SCHEMA_VIOLATION)

        assert isinstance(result, str), "the raw-text degradation must survive"
        assert result.failure_kind == "validation"
        assert result.validation_error.errors()[0]["type"] == "string_type"

    @pytest.mark.asyncio
    async def test_raise_names_the_schema_not_the_provider(self):
        """A schema rejection must not be blamed on structured-output support."""
        with pytest.raises(ValueError) as exc_info:
            await self._operate(FENCED_SCHEMA_VIOLATION, handle_validation="raise")

        exc = exc_info.value
        assert isinstance(exc, SchemaRejectedError)
        assert exc.validation_error is not None
        assert "courses.2.name" in str(exc)
        assert "supports structured JSON output" not in str(exc)

    @pytest.mark.asyncio
    async def test_unparseable_text_still_blames_extraction(self):
        with pytest.raises(ValueError) as exc_info:
            await self._operate(NOTHING_PARSEABLE, handle_validation="raise")

        assert isinstance(exc_info.value, ExtractionError)
        assert exc_info.value.validation_error is None


class TestUnparsedResponseIsStillAnOrdinaryString:
    """The give-up path used to return a plain str, and callers rely on that.

    Carrying the failure reason is only free if nothing a caller already did to
    the returned text stops working. Copying is the case that is easy to miss:
    a str subclass is rebuilt by calling ``__new__`` with the arguments
    ``__getnewargs_ex__`` reports, so a keyword-only ``__new__`` without it
    raises on ``copy``, ``deepcopy`` and ``pickle`` alike -- while the plain str
    it replaced survives all three.
    """

    def _response(self, error: Exception | None = None) -> UnparsedResponse:
        return UnparsedResponse(
            "some model text", failure_kind="validation", validation_error=error
        )

    @pytest.mark.parametrize(
        "roundtrip",
        [copy.copy, copy.deepcopy, lambda value: pickle.loads(pickle.dumps(value))],
        ids=["copy", "deepcopy", "pickle"],
    )
    def test_a_roundtrip_keeps_the_text_and_the_reason(self, roundtrip):
        original = self._response(ValueError("boom"))
        restored = roundtrip(original)

        assert restored == original
        assert type(restored) is UnparsedResponse
        assert restored.failure_kind == "validation"
        assert isinstance(restored.validation_error, ValueError)

    @pytest.mark.parametrize(
        "roundtrip",
        [copy.copy, copy.deepcopy, lambda value: pickle.loads(pickle.dumps(value))],
        ids=["copy", "deepcopy", "pickle"],
    )
    def test_a_roundtrip_survives_a_real_pydantic_error(self, roundtrip):
        # The error actually attached in production is a pydantic
        # ValidationError, not a ValueError anyone constructed in a test.
        try:
            Course.model_validate({"name": None, "credits": 3})
        except Exception as validation_error:
            original = self._response(validation_error)

        restored = roundtrip(original)
        assert restored == original
        assert "name" in str(restored.validation_error)

    def test_it_behaves_as_the_plain_string_it_replaced(self):
        raw = "some model text"
        response = self._response()

        assert isinstance(response, str)
        assert (response, hash(response), len(response)) == (raw, hash(raw), len(raw))
        assert f"{response}" == raw
        assert json.dumps(response) == json.dumps(raw)
        assert {raw: "value"}[response] == "value"


class TestEmptyText:
    """A reply with no text has nothing a reformat turn could recover."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", ["", "  \n\t"])
    async def test_no_reformat_turn_is_sent_for_an_empty_reply(self, body):
        branch = Branch()
        sent = []

        async def fake_chat(*_a, **_kw):
            sent.append(1)
            raise AssertionError("a reformat turn was sent for an empty reply")

        param = make_parse_param(Transcript, None)
        with patch.object(Branch, "chat", fake_chat):
            result = await _parse(branch, body, param)

        assert sent == []
        assert isinstance(result, UnparsedResponse)
        assert result == body
        assert result.failure_kind == "extraction"

    @pytest.mark.asyncio
    async def test_an_empty_reply_raises_an_extraction_error_when_asked_to(self):
        branch = Branch()

        async def fake_chat(*_a, **_kw):
            raise AssertionError("a reformat turn was sent for an empty reply")

        param = make_parse_param(Transcript, None, handle_validation="raise")
        with patch.object(Branch, "chat", fake_chat):
            with pytest.raises(ExtractionError, match="no text to parse"):
                await _parse(branch, "", param)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", ["", "   "])
    @pytest.mark.parametrize("fuzzy", [True, False], ids=["fuzzy-keys", "no-fuzzy-keys"])
    async def test_an_empty_reply_is_an_extraction_failure_whatever_its_schema(self, body, fuzzy):
        # Without fuzzy key matching the first attempt reads no text as a value the schema
        # rejects; the outcome still says there was nothing to parse.
        class Count(BaseModel):
            n: int

        def param(mode):
            if fuzzy:
                return make_parse_param(Count, None, handle_validation=mode)
            return ParseParam(response_format=Count, handle_validation=mode)

        branch = Branch()

        async def fake_chat(*_a, **_kw):
            raise AssertionError("a reformat turn was sent for an empty reply")

        with patch.object(Branch, "chat", fake_chat):
            with pytest.raises(ExtractionError, match="no text to parse") as raised:
                await _parse(branch, body, param("raise"))
            unparsed = await _parse(branch, body, param("return_value"))

        assert type(raised.value) is ExtractionError
        assert isinstance(unparsed, UnparsedResponse)
        assert unparsed.failure_kind == "extraction"
        assert unparsed.validation_error is None


_CUT_OFF_TEXT = "Let me think about the transcript first. The student is A. Rivera and"

# One reply text in each provider shape: as a response that stopped at its output token
# limit, and as one that ended normally.
_OUTPUT_LIMIT_REPLIES = {
    "anthropic-max-tokens": lambda body: {
        "type": "message",
        "role": "assistant",
        "stop_reason": "max_tokens",
        "content": [{"type": "text", "text": body}],
    },
    "chat-completions-length": lambda body: {
        "choices": [
            {
                "index": 0,
                "finish_reason": "length",
                "message": {"role": "assistant", "content": body},
            }
        ]
    },
    "responses-incomplete": lambda body: {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": body}]}],
    },
}
_ENDED_REPLIES = {
    "anthropic-end-turn": lambda body: {
        "type": "message",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": body}],
    },
    "chat-completions-stop": lambda body: {
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": body},
            }
        ]
    },
    "responses-completed": lambda body: {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": body}]}],
    },
}


async def _communicate_on(body: str, raw: dict, sent: list, *, mode: str = "raise", repair=None):
    """Run communicate() on a first reply whose provider response is ``raw``.

    The first turn and the repair request are stubbed. Each repair request sent is counted in
    ``sent`` and answers with ``repair`` as its text; without ``repair`` it fails.
    """
    branch = Branch()

    async def first_turn(branch_, instruction, chat_param, **_kw):
        return (
            branch_.msgs.create_instruction(instruction="extract the transcript"),
            AssistantResponse(
                role="assistant",
                content={"assistant_response": body},
                sender=branch_.id,
                recipient=branch_.user,
                metadata={"model_response": raw},
            ),
        )

    async def repair_turn(*_a, **_kw):
        sent.append(1)
        if repair is None:
            raise AssertionError("a repair request was sent")
        return None, AssistantResponse(
            role="assistant",
            content={"assistant_response": repair},
            sender=branch.id,
            recipient=branch.user,
        )

    kw = prepare_communicate_kw(
        branch,
        "extract the transcript",
        response_format=Transcript,
        num_parse_retries=1,
        fuzzy_match_kwargs={"handle_validation": mode},
    )
    chat_module = importlib.import_module("lionagi.operations.chat.chat")
    with patch.object(chat_module, "chat", first_turn):
        with patch.object(Branch, "chat", repair_turn):
            return await communicate(branch, **kw)


class TestReplyCutOffAtItsOutputLimit:
    """A reply that stopped at its output token limit is not sent to a repair request.

    The reply is incomplete, so asking a model to reformat it cannot recover the missing text.
    The parse goes straight to its failure handling and says the reply stopped at its limit.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make_reply", _OUTPUT_LIMIT_REPLIES.values(), ids=list(_OUTPUT_LIMIT_REPLIES)
    )
    async def test_no_repair_request_is_sent_and_the_reason_names_the_stop(self, make_reply):
        sent: list = []

        with pytest.raises(ValueError, match="stopped at its output token limit") as raised:
            await _communicate_on(_CUT_OFF_TEXT, make_reply(_CUT_OFF_TEXT), sent)

        assert sent == []
        assert type(raised.value.__cause__) is ExtractionError

    @pytest.mark.asyncio
    async def test_return_none_gives_none_without_a_repair_request(self):
        sent: list = []
        raw = _OUTPUT_LIMIT_REPLIES["anthropic-max-tokens"](_CUT_OFF_TEXT)

        outcome = await _communicate_on(_CUT_OFF_TEXT, raw, sent, mode="return_none")

        assert sent == []
        assert outcome is None

    @pytest.mark.asyncio
    async def test_return_value_gives_the_unparsed_text_without_a_repair_request(self):
        sent: list = []
        raw = _OUTPUT_LIMIT_REPLIES["anthropic-max-tokens"](_CUT_OFF_TEXT)

        outcome = await _communicate_on(_CUT_OFF_TEXT, raw, sent, mode="return_value")

        assert sent == []
        assert isinstance(outcome, UnparsedResponse)
        assert outcome == _CUT_OFF_TEXT
        assert outcome.failure_kind == "extraction"
        assert outcome.validation_error is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("make_reply", _ENDED_REPLIES.values(), ids=list(_ENDED_REPLIES))
    async def test_a_reply_that_ended_normally_still_gets_the_repair_request(self, make_reply):
        sent: list = []

        outcome = await _communicate_on(
            _CUT_OFF_TEXT, make_reply(_CUT_OFF_TEXT), sent, repair=FENCED_CONFORMANT
        )

        assert len(sent) == 1
        assert isinstance(outcome, Transcript)

    @pytest.mark.asyncio
    async def test_a_complete_reply_cut_off_at_its_limit_still_parses(self):
        # The stop only ends the repair; a reply that already parses is used as it is.
        sent: list = []
        raw = _OUTPUT_LIMIT_REPLIES["anthropic-max-tokens"](FENCED_CONFORMANT)

        outcome = await _communicate_on(FENCED_CONFORMANT, raw, sent)

        assert sent == []
        assert isinstance(outcome, Transcript)
