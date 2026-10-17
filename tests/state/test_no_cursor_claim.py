# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""``NO_CURSOR_CLAIM`` stays the one object callers compare against by identity."""

from __future__ import annotations

import copy

from lionagi.state.db import NO_CURSOR_CLAIM, NoCursorClaim


def test_constructing_the_claim_type_returns_the_module_sentinel():
    assert NoCursorClaim() is NO_CURSOR_CLAIM


def test_copies_of_the_sentinel_are_the_sentinel():
    assert copy.copy(NO_CURSOR_CLAIM) is NO_CURSOR_CLAIM
    assert copy.deepcopy({"claim": NO_CURSOR_CLAIM})["claim"] is NO_CURSOR_CLAIM


def test_the_sentinel_is_truthy_and_names_itself():
    assert bool(NO_CURSOR_CLAIM) is True
    assert repr(NO_CURSOR_CLAIM) == "NO_CURSOR_CLAIM"
