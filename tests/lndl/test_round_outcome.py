# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lionagi.lndl.round_outcome — the RoundOutcome ADT (ADR-0024 §2)."""

import dataclasses

import pytest

from lionagi.lndl.round_outcome import Continue, Retry, Success


class TestSuccess:
    def test_construction_and_field(self):
        outcome = Success(output={"answer": "hi"})
        assert outcome.output == {"answer": "hi"}

    def test_frozen(self):
        outcome = Success(output=1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.output = 2


class TestContinue:
    def test_default_notes_committed_is_empty_tuple(self):
        outcome = Continue()
        assert outcome.notes_committed == ()

    def test_notes_committed_accepts_tuple(self):
        outcome = Continue(notes_committed=("outline", "draft"))
        assert outcome.notes_committed == ("outline", "draft")

    def test_frozen(self):
        outcome = Continue()
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.notes_committed = ("x",)


class TestRetry:
    def test_construction_requires_error(self):
        outcome = Retry(error="undeclared alias 'x'")
        assert outcome.error == "undeclared alias 'x'"
        assert outcome.note_keys == ()

    def test_note_keys_accepts_tuple(self):
        outcome = Retry(error="bad", note_keys=("a", "b"))
        assert outcome.note_keys == ("a", "b")

    def test_frozen(self):
        outcome = Retry(error="bad")
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.error = "other"


class TestRoundOutcomeDispatch:
    """RoundOutcome is a plain union type alias, not a base class — callers
    dispatch via isinstance() against the three variants directly."""

    @pytest.mark.parametrize(
        "outcome,expected_type",
        [
            (Success(output=1), Success),
            (Continue(), Continue),
            (Retry(error="e"), Retry),
        ],
    )
    def test_isinstance_matches_exactly_one_variant(self, outcome, expected_type):
        variants = (Success, Continue, Retry)
        matches = [v for v in variants if isinstance(outcome, v)]
        assert matches == [expected_type]
