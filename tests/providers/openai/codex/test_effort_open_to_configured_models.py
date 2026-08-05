# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The codex request does not gate reasoning effort against a closed set.

codex reaches models other than OpenAI's through the ``model_providers``
tables in ``~/.codex/config.toml``. Those models carry their own effort
vocabularies, and the value here is emitted verbatim as
``-c reasoning_effort=<val>`` for the CLI to interpret, so codex and the
provider behind it are the authority on what is valid. A closed set in this
layer would reject a working configuration before the CLI ever saw it.

What these tests hold down is both halves of that: an unrecognised word is
accepted *and reaches argv*, and lionagi's own vocabulary keeps working.
Acceptance alone would be satisfied by a field that swallows the value.
"""

from __future__ import annotations

import pytest
import toml

from lionagi.providers.openai.codex import (
    CODEX_REASONING_EFFORTS,
    CodexCodeRequest,
)
from lionagi.service.providers import EFFORT_LEVELS

# A word deliberately outside lionagi's vocabulary, standing in for whatever a
# model configured behind codex calls its own tiers.
VENDOR_EFFORT = "deepthink"


def _c_overrides(args: list[str]) -> list[str]:
    """The ``-c key=value`` values codex actually receives.

    Scanning stops at ``--``: everything after it is the prompt argument, so a
    pair emitted past the marker never reaches codex as configuration. A helper
    that scanned the whole list would report such a pair as delivered.
    """
    end = args.index("--") if "--" in args else len(args)
    head = args[:end]
    return [head[i + 1] for i, a in enumerate(head[:-1]) if a == "-c"]


def _c_values(args: list[str], key: str) -> list[str]:
    """Every value emitted for *key*, decoded from its TOML literal.

    A list rather than a single value on purpose: an assertion that some pair
    is *present* is equally satisfied by two of them, and codex resolves a
    repeated key by taking the last, so duplicate emission is a real way to
    ship the wrong setting past a passing test.
    """
    out = []
    for pair in _c_overrides(args):
        pair_key, _, raw = pair.partition("=")
        if pair_key == key:
            out.append(toml.loads(f"v = {raw}")["v"])
    return out


def _c_value(args: list[str], key: str) -> str:
    values = _c_values(args, key)
    assert len(values) == 1, f"expected exactly one {key} override, got {values}"
    return values[0]


class TestAnUnrecognisedEffortReachesTheCLI:
    def test_a_vendor_effort_word_is_accepted(self):
        req = CodexCodeRequest(prompt="hello", reasoning_effort=VENDOR_EFFORT)
        assert req.reasoning_effort == VENDOR_EFFORT

    def test_and_is_emitted_rather_than_swallowed(self):
        """Acceptance without emission would pass a looser type check and still
        drop the caller's setting on the floor."""
        req = CodexCodeRequest(prompt="hello", reasoning_effort=VENDOR_EFFORT)
        assert _c_value(req.as_cmd_args(), "reasoning_effort") == VENDOR_EFFORT

    def test_plan_mode_effort_is_open_the_same_way(self):
        req = CodexCodeRequest(prompt="hello", plan_mode_reasoning_effort=VENDOR_EFFORT)
        assert _c_value(req.as_cmd_args(), "plan_mode_reasoning_effort") == VENDOR_EFFORT

    def test_a_model_id_from_another_vendor_is_accepted(self):
        """The reason the effort set had to open: these models are configured
        into codex, and they are not named like OpenAI's."""
        req = CodexCodeRequest(prompt="hello", model="deepseek/deepseek-v4-flash-0731")
        assert req.model == "deepseek/deepseek-v4-flash-0731"
        assert "deepseek/deepseek-v4-flash-0731" in req.as_cmd_args()


class TestLionagisOwnVocabularyStillWorks:
    """Opening the set must not stop the eight words lionagi produces from
    being carried, which is the regression an over-broad change would cause."""

    @pytest.mark.parametrize("effort", CODEX_REASONING_EFFORTS)
    def test_each_documented_effort_reaches_argv(self, effort):
        # Against a model whose ceiling admits every tier. The default model
        # clamps max/ultra down to xhigh, so using it here would test the
        # clamp table rather than whether the value is carried.
        req = CodexCodeRequest(prompt="hello", model="gpt-5.6-sol", reasoning_effort=effort)
        assert _c_value(req.as_cmd_args(), "reasoning_effort") == effort

    @pytest.mark.parametrize("effort", ["max", "ultra"])
    def test_a_known_models_ceiling_still_clamps(self, effort):
        """The counterpart to the arm above: opening the type did not disable
        the clamp, which is a separate mechanism keyed on the model."""
        req = CodexCodeRequest(prompt="hello", model="gpt-5.3-codex", reasoning_effort=effort)
        assert _c_value(req.as_cmd_args(), "reasoning_effort") == "xhigh"

    def test_a_configured_model_is_not_clamped_against_openais_ceilings(self):
        """A model reached through codex's own provider tables is unknown to
        the clamp tables, and must keep the effort it was given rather than
        inherit a ceiling that belongs to a different vendor's model."""
        req = CodexCodeRequest(
            prompt="hello",
            model="deepseek/deepseek-v4-flash-0731",
            reasoning_effort="max",
        )
        assert _c_value(req.as_cmd_args(), "reasoning_effort") == "max"

    def test_no_effort_emits_no_override(self):
        args = CodexCodeRequest(prompt="hello").as_cmd_args()
        assert _c_values(args, "reasoning_effort") == []

    def test_no_plan_mode_effort_emits_no_plan_mode_override(self):
        """Its own arm: the two fields are emitted by separate branches, and an
        unset one leaking a default would put a setting on the CLI that no
        caller asked for."""
        args = CodexCodeRequest(prompt="hello", reasoning_effort="high").as_cmd_args()
        assert _c_values(args, "plan_mode_reasoning_effort") == []


class TestTheEffortIsEncodedLikeEveryOtherOverride:
    """``-c key=value`` values are TOML literals, and every other override goes
    through the same encoder. While the field was a closed set of eight bare
    words, emitting it unencoded was safe by construction. Opening the type
    removed that guarantee, so what stops an arbitrary value from meaning
    something else is the encoding rather than codex's tolerance.
    """

    @pytest.mark.parametrize(
        "value",
        [
            'high"\nsandbox_mode = "danger-full-access',
            '"high"\nmodel_provider = "elsewhere"',
            "with spaces",
            'embedded"quote',
            "trailing\\",
        ],
    )
    def test_a_crafted_value_stays_inside_its_own_override(self, value):
        req = CodexCodeRequest(prompt="hello", model="gpt-5.6-sol", reasoning_effort=value)
        pair = next(p for p in _c_overrides(req.as_cmd_args()) if p.startswith("reasoning_effort="))
        # Read the whole pair the way a TOML document would: it must define the
        # one key, carrying exactly the string given, and nothing else.
        assert toml.loads(pair) == {"reasoning_effort": value}

    def test_the_ordinary_case_is_a_quoted_string(self):
        """Pins the wire form, so the encoding cannot be dropped while the
        decoded assertions above keep passing on a raw value."""
        req = CodexCodeRequest(prompt="hello", model="gpt-5.6-sol", reasoning_effort="high")
        assert 'reasoning_effort="high"' in _c_overrides(req.as_cmd_args())


class TestTheDocumentedVocabularyStaysInStepWithLionagis:
    def test_it_matches_the_effort_levels_the_rest_of_lionagi_uses(self):
        """``CODEX_REASONING_EFFORTS`` is documentation now that the field is
        open, so nothing would fail if it silently drifted from the set the
        CLI and specs validate against. This is what notices."""
        assert set(CODEX_REASONING_EFFORTS) == set(EFFORT_LEVELS)
