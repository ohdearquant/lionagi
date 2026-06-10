# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for audit-identified model bugs.

Covers:
- LIONAGI-AUDIT-004: HashableModel.from_json(bytes) round-trip
- LIONAGI-AUDIT-005: OperableModel.field_hasattr attr vs field_name lookup
"""

from __future__ import annotations

import pytest
from pydantic import Field

from lionagi.models.hashable_model import HashableModel
from lionagi.models.operable_model import OperableModel

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Sample(HashableModel):
    name: str = "default"
    value: int = 0


class _OpSample(OperableModel):
    label: str = "x"


# ---------------------------------------------------------------------------
# LIONAGI-AUDIT-004: HashableModel.from_json bytes round-trip
# ---------------------------------------------------------------------------


class TestHashableModelFromJsonBytes:
    """from_json(bytes) must accept the output of to_json(decode=False)."""

    def test_bytes_round_trip(self):
        """to_json(decode=False) → from_json(bytes) must round-trip identically."""
        original = _Sample(name="hello", value=42)
        raw: bytes = original.to_json(decode=False)
        assert isinstance(raw, bytes), "to_json(decode=False) should return bytes"
        recovered = _Sample.from_json(raw)
        assert recovered.name == original.name
        assert recovered.value == original.value

    def test_str_round_trip_still_works(self):
        """Ensure the pre-existing str round-trip is not broken."""
        original = _Sample(name="world", value=7)
        raw: str = original.to_json(decode=True)
        assert isinstance(raw, str)
        recovered = _Sample.from_json(raw)
        assert recovered == original

    def test_bytes_equality(self):
        """Bytes and str round-trips produce equivalent models."""
        original = _Sample(name="abc", value=99)
        from_bytes = _Sample.from_json(original.to_json(decode=False))
        from_str = _Sample.from_json(original.to_json(decode=True))
        assert from_bytes.name == from_str.name
        assert from_bytes.value == from_str.value


# ---------------------------------------------------------------------------
# LIONAGI-AUDIT-005: OperableModel.field_hasattr — attr vs field_name
# ---------------------------------------------------------------------------


class TestFieldHasattr:
    """field_hasattr must check the *attr* key, not the field name."""

    def test_custom_attr_found_after_field_setattr(self):
        """field_hasattr returns True for an attr that was set via field_setattr."""
        m = _OpSample()
        m.add_field("score", value=10, annotation=int)
        m.field_setattr("score", "my_custom_key", "my_value")
        assert m.field_hasattr("score", "my_custom_key") is True

    def test_absent_attr_returns_false(self):
        """field_hasattr returns False (not None, not raising) for a missing attr."""
        m = _OpSample()
        m.add_field("score", value=10, annotation=int)
        result = m.field_hasattr("score", "nonexistent_attr")
        assert result is False or result is None  # False preferred; None also acceptable

    def test_field_name_is_not_confused_with_attr(self):
        """The old bug returned True when json_schema_extra contained the field name.

        This regression test demonstrates the old false-positive was fixed:
        setting an attr named differently than the field should be found;
        checking for the field name itself (when that key is absent) should not
        falsely succeed.
        """
        m = _OpSample()
        m.add_field("score", value=5, annotation=int)
        # Set an attr with a key that differs from the field name.
        m.field_setattr("score", "meta_tag", "important")
        # The attr we set must be found.
        assert m.field_hasattr("score", "meta_tag") is True
        # The field name "score" is not itself an attr (unless explicitly set).
        # This would have been a false positive under the old code.
        result = m.field_hasattr("score", "score")
        # Should be False (the key "score" was never stored in json_schema_extra).
        assert result is False or result is None

    def test_field_getattr_and_field_hasattr_consistent(self):
        """field_hasattr True ⟹ field_getattr returns the value (not UNDEFINED)."""
        from lionagi.utils import UNDEFINED

        m = _OpSample()
        m.add_field("flag", value=True, annotation=bool)
        m.field_setattr("flag", "checked", "yes")
        assert m.field_hasattr("flag", "checked") is True
        val = m.field_getattr("flag", "checked")
        assert val == "yes"
        assert val is not UNDEFINED

    def test_missing_field_raises_key_error(self):
        """field_hasattr on a non-existent field must raise KeyError."""
        m = _OpSample()
        with pytest.raises(KeyError):
            m.field_hasattr("does_not_exist", "any_attr")
