# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the cognitive mode model (lionagi/casts/mode.py)."""

import pytest
from pydantic import ValidationError

from lionagi.casts.mode import (
    Mode,
    ModeAxis,
    ModeConflictError,
    builtin_modes,
    get_mode,
    load_mode_file,
    validate_mode_stack,
)


def test_all_fourteen_modes_load():
    modes = builtin_modes()
    assert len(modes) == 14
    assert sum(m.tier == "core" for m in modes.values()) == 11
    assert sum(m.tier == "extended" for m in modes.values()) == 3
    for m in modes.values():
        assert m.kind == "mode"
        assert m.prompt and m.description
        assert m.when_to_use and m.when_not_to_use
        assert isinstance(m.axis, ModeAxis)


def test_kind_is_enforced_at_construction():
    # Regression: a frozen field blocks reassignment but not constructor input;
    # kind must be a Literal so a wrong kind is rejected up front.
    assert Mode(name="x").kind == "mode"
    with pytest.raises(ValidationError):
        Mode(name="x", kind="role")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capabilities": frozenset({"chat"})},
        {"resources": frozenset({"bash"})},
        {"authority": ("approve",)},
        {"boundaries": ("cannot do x",)},
        {"extra": {"authority": ["approve"]}},  # smuggling via the escape hatch
    ],
)
def test_purity_contract_rejects_noncognitive_fields(kwargs):
    with pytest.raises(ValidationError):
        Mode(name="x", **kwargs)


def test_hard_conflicts_raise():
    for a, b in [("fast", "slow"), ("fast", "systematic")]:
        with pytest.raises(ModeConflictError):
            validate_mode_stack([get_mode(a), get_mode(b)])


def test_legal_stacks_have_no_conflict():
    assert (
        validate_mode_stack([get_mode("evidential"), get_mode("probabilistic"), get_mode("slow")])
        == []
    )
    assert validate_mode_stack([get_mode("adversarial"), get_mode("evidential")]) == []


def test_same_axis_crowding_warns_but_does_not_raise():
    warnings = validate_mode_stack(
        [get_mode("systematic"), get_mode("framing"), get_mode("associative")]
    )
    assert warnings and "search-topology" in warnings[0]


def test_registry_returns_independent_instances():
    # Regression: cached built-ins must not be shared. Each lookup is a fresh
    # deep copy, so a caller can never poison the canonical instance.
    assert get_mode("fast") is not get_mode("fast")


def test_extra_is_read_only_after_construction():
    # Regression: 'frozen=True' blocks reassignment but not in-place dict
    # mutation; a mode must not be able to acquire metadata after construction.
    mode = get_mode("fast")
    with pytest.raises(TypeError):
        mode.extra["authority"] = ["approve"]


@pytest.mark.parametrize(
    "update",
    [
        {"extra": {"authority": ["approve"]}},
        {"capabilities": frozenset({"shell"})},
    ],
)
def test_model_copy_update_cannot_break_purity(update):
    # Regression: pydantic's model_copy(update=...) bypasses validators; the
    # override re-validates so a copy cannot smuggle in non-cognitive fields.
    with pytest.raises(ValidationError):
        get_mode("fast").model_copy(update=update)


def test_loader_rejects_unknown_frontmatter_keys(tmp_path):
    # Regression: the loader is fail-closed — a forbidden or typo'd frontmatter
    # key must reject the file, not be silently dropped into a "pure" mode.
    bad = tmp_path / "bad.md"
    bad.write_text(
        "---\n"
        "name: bad\n"
        "axis: tempo\n"
        "authority: [approve]\n"
        "artifacts: [report]\n"
        "---\n\n"
        "**Description**: smuggled\n\n"
        "## Behavioral Instructions\n\nThink.\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported frontmatter keys"):
        load_mode_file(bad)


def test_get_mode_unknown_raises():
    with pytest.raises(KeyError):
        get_mode("does-not-exist")
