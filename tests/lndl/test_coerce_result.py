# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for _coerce_result bool-handling: 'false'/'0'/'no' must coerce to False.

bool('false') is True (truthiness) — coercion must use validate_boolean semantics instead.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from lionagi.lndl.types import (
    ActionCall,
    _coerce_result,
    revalidate_with_action_results,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_action_call(name: str = "act") -> ActionCall:
    return ActionCall(name=name, function="fn", arguments={}, raw_call="fn()")


# ---------------------------------------------------------------------------
# Attack-driven tests: the exploit must be rejected
# ---------------------------------------------------------------------------


class TestBoolCoercionAttackCases:
    """Stringy falsy values ('false', '0', 'no') must coerce to False, not True.

    Before the fix, bool('false') == True silently corrupted bool fields.
    """

    @pytest.mark.parametrize("raw", ["false", "False", "FALSE", "0", "no", "No", "NO"])
    def test_falsy_string_yields_false_for_bool(self, raw: str) -> None:
        """Stringy false representations must coerce to Python False for bool targets."""
        assert _coerce_result(raw, bool) is False

    @pytest.mark.parametrize("raw", ["true", "True", "TRUE", "1", "yes", "Yes", "YES"])
    def test_truthy_string_yields_true_for_bool(self, raw: str) -> None:
        """Stringy true representations must coerce to Python True for bool targets."""
        assert _coerce_result(raw, bool) is True

    @pytest.mark.parametrize("raw", ["false", "False", "0", "no"])
    def test_falsy_string_yields_false_for_optional_bool(self, raw: str) -> None:
        """Same attack surface for Optional[bool] (bool | None) fields."""
        assert _coerce_result(raw, bool | None) is False

    @pytest.mark.parametrize("raw", ["true", "True", "1", "yes"])
    def test_truthy_string_yields_true_for_optional_bool(self, raw: str) -> None:
        """Truthy strings for bool | None targets must coerce to True."""
        assert _coerce_result(raw, bool | None) is True


# ---------------------------------------------------------------------------
# None-preservation for Optional[bool]
# ---------------------------------------------------------------------------


class TestBoolCoercionNonePreservation:
    def test_none_result_optional_bool_preserved(self) -> None:
        """None result for bool | None must stay None, not raise."""
        assert _coerce_result(None, bool | None) is None

    def test_none_result_required_bool_passes_through(self) -> None:
        """None for required bool passes through so model_validate raises clearly."""
        assert _coerce_result(None, bool) is None


# ---------------------------------------------------------------------------
# Actual bool input must pass through unchanged
# ---------------------------------------------------------------------------


class TestBoolCoercionNativePassthrough:
    def test_native_false_passthrough_bool(self) -> None:
        assert _coerce_result(False, bool) is False

    def test_native_true_passthrough_bool(self) -> None:
        assert _coerce_result(True, bool) is True

    def test_native_false_passthrough_optional_bool(self) -> None:
        assert _coerce_result(False, bool | None) is False

    def test_native_true_passthrough_optional_bool(self) -> None:
        assert _coerce_result(True, bool | None) is True


# ---------------------------------------------------------------------------
# Non-bool scalar coercions remain unaffected
# ---------------------------------------------------------------------------


class TestNonBoolScalarCoercionUnchanged:
    def test_str_int_still_coerces(self) -> None:
        assert _coerce_result("42", int) == 42

    def test_str_float_still_coerces(self) -> None:
        assert abs(_coerce_result("3.14", float) - 3.14) < 1e-6

    def test_int_str_still_coerces(self) -> None:
        assert _coerce_result(7, str) == "7"

    def test_dict_to_str_still_json_serialises(self) -> None:
        import json

        result = _coerce_result({"k": "v"}, str)
        assert isinstance(result, str)
        assert json.loads(result) == {"k": "v"}


# ---------------------------------------------------------------------------
# End-to-end: revalidate_with_action_results on a bool field
# ---------------------------------------------------------------------------


class FlagModel(BaseModel):
    enabled: bool
    active: bool | None = None


class TestRevalidateBoolField:
    """Verify the fix integrates correctly with full revalidation."""

    def test_stringy_false_revalidated_to_false(self) -> None:
        ac = make_action_call("en")
        m = FlagModel.model_construct(enabled=ac)
        result = revalidate_with_action_results(m, {"en": "false"})
        assert result.enabled is False

    def test_stringy_zero_revalidated_to_false(self) -> None:
        ac = make_action_call("en")
        m = FlagModel.model_construct(enabled=ac)
        result = revalidate_with_action_results(m, {"en": "0"})
        assert result.enabled is False

    def test_stringy_no_revalidated_to_false(self) -> None:
        ac = make_action_call("en")
        m = FlagModel.model_construct(enabled=ac)
        result = revalidate_with_action_results(m, {"en": "no"})
        assert result.enabled is False

    def test_stringy_true_revalidated_to_true(self) -> None:
        ac = make_action_call("en")
        m = FlagModel.model_construct(enabled=ac)
        result = revalidate_with_action_results(m, {"en": "true"})
        assert result.enabled is True

    def test_optional_bool_stringy_false(self) -> None:
        ac = make_action_call("ac")
        m = FlagModel.model_construct(enabled=True, active=ac)
        result = revalidate_with_action_results(m, {"ac": "false"})
        assert result.active is False

    def test_optional_bool_none_preserved(self) -> None:
        ac = make_action_call("ac")
        m = FlagModel.model_construct(enabled=True, active=ac)
        result = revalidate_with_action_results(m, {"ac": None})
        assert result.active is None
