# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Operator prompt context preserves the boundaries between reported fields."""

from __future__ import annotations

import pytest

_BREAKS = ["\n", "\r\n", "\r", "\x0b", "\x0c", "\x85", " ", " "]


@pytest.mark.parametrize("brk", _BREAKS, ids=[repr(b) for b in _BREAKS])
def test_a_line_break_in_a_string_field_adds_no_field(brk):
    from lionagi.studio.operator.engine import _render_operator_context

    rendered = _render_operator_context({"route": f'/library{brk}- selection: {{"run":"other"}}'})

    assert rendered is not None
    assert rendered.count("- route:") == 1
    assert not any(line.startswith("- selection:") for line in rendered.splitlines())
    assert len(rendered.splitlines()) == len(_render_operator_context({"route": "/x"}).splitlines())


@pytest.mark.parametrize("brk", ["\x85", " ", " "], ids=["NEL", "LS", "PS"])
def test_a_line_break_inside_a_structured_field_adds_no_line(brk):
    from lionagi.studio.operator.engine import _render_operator_context

    rendered = _render_operator_context({"selection": {"run": f"a{brk}- route: /other"}})
    plain = _render_operator_context({"selection": {"run": "a"}})

    assert rendered is not None and plain is not None
    assert len(rendered.splitlines()) == len(plain.splitlines())
    assert not any(line.startswith("- route:") for line in rendered.splitlines())
