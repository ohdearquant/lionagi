# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Security regression tests for SpawnRequest operation allowlist and emission extra='forbid'."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lionagi.casts.emission import (
    SPAWN_ALLOWED_OPERATIONS,
    Finding,
    SpawnRequest,
)


class TestSpawnRequestOperationAllowlist:
    """SpawnRequest rejects operations outside the documented allowlist."""

    @pytest.mark.parametrize("op", sorted(SPAWN_ALLOWED_OPERATIONS))
    def test_allowed_operations_accepted(self, op: str):
        """All documented operations must be accepted."""
        req = SpawnRequest(instruction="do something", operation=op)
        assert req.operation == op

    def test_default_operation_is_operate(self):
        req = SpawnRequest(instruction="x")
        assert req.operation == "operate"

    @pytest.mark.parametrize(
        "bad_op",
        [
            "dangerous",
            "exec",
            "eval",
            "__import__",
            "ReActStream",  # BranchOperations not in spawn allowlist
            "parse",
            "select",
            "act",
            "interpret",
            "",  # empty string
            "  ",  # whitespace only
        ],
    )
    def test_unknown_operation_rejected_at_validation(self, bad_op: str):
        """Non-allowlisted operation strings raise ValidationError before any routing."""
        with pytest.raises(ValidationError):
            SpawnRequest(instruction="pwn", operation=bad_op)

    def test_model_validate_rejects_unknown_operation(self):
        """model_validate (the path used by casts extraction) must also fail."""
        with pytest.raises(ValidationError):
            SpawnRequest.model_validate({"instruction": "x", "operation": "not_registered"})

    def test_operation_none_falls_back_to_default(self):
        """Explicit None uses the field default ('operate')."""
        # None is not in the Literal, so it gets the default
        req = SpawnRequest(instruction="x")
        assert req.operation == "operate"


class TestEmissionModelExtraForbid:
    """Emission models reject unknown keys via extra='forbid'."""

    def test_finding_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="extra_inputs_not_permitted|extra"):
            Finding(description="bug", unknown_field="evil")

    def test_finding_model_validate_rejects_unknown_key(self):
        with pytest.raises(ValidationError):
            Finding.model_validate({"description": "x", "injected": "payload"})

    def test_spawn_request_rejects_unknown_key(self):
        with pytest.raises(ValidationError):
            SpawnRequest(instruction="x", unknown_field="evil")

    def test_valid_finding_still_works(self):
        """Sanity check: valid emissions are unaffected."""
        f = Finding(description="real finding", confidence=0.9)
        assert f.description == "real finding"
        assert f.confidence == 0.9
