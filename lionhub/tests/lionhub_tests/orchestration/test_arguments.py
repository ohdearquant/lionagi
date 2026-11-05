# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Argument types declare and enforce the values they accept."""

from __future__ import annotations

import argparse

import pytest

from lionhub.orchestration.arguments import BoundedInt


def test_the_bounded_type_states_its_range():
    kind = BoundedInt(1, 60)
    assert kind.minimum == 1 and kind.maximum == 60
    assert kind("60") == 60
    with pytest.raises(argparse.ArgumentTypeError, match="from 1 to 60"):
        kind("61")
    with pytest.raises(argparse.ArgumentTypeError, match="whole number"):
        kind("x")
