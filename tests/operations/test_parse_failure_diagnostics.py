# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A failed parse has two very different causes, and they need different fixes.

Either no JSON could be recovered from the text, or JSON was recovered intact
and the response model refused it. These tests pin that the fenced-block
extraction path works, that the two causes stay distinguishable, and that the
reformat retries do not reissue an identical request.
"""

from unittest.mock import patch

import pytest
from pydantic import BaseModel

from lionagi.operations._defaults import make_parse_param
from lionagi.operations.parse import (
    ExtractionError,
    SchemaRejectedError,
    UnparsedResponse,
)
from lionagi.operations.parse.parse import _validate_dict_or_model
from lionagi.operations.parse.parse import parse as _parse
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
