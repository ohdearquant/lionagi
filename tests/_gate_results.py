# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Gate verdicts built around the constructor, for tests of the adapters' own checks."""

from __future__ import annotations

from dataclasses import fields

from lionagi.protocols.action.gate import GateResult


def built_around_construction(base: GateResult, **changes) -> GateResult:
    """A copy of *base* with *changes* set by ``object.__setattr__`` on an instance made by
    ``object.__new__``, so it can break the rules construction enforces, as a third-party
    instance can. Every field of ``GateResult`` is copied, so a field added later is set too."""
    result = object.__new__(GateResult)
    for field in fields(GateResult):
        object.__setattr__(result, field.name, changes.get(field.name, getattr(base, field.name)))
    return result
