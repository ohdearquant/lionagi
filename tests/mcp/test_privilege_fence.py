# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Operations that grant privilege stay off this surface.

Every caller here is an agent. Trusting a plugin lets a bundle run code in the
process, and migrating the store rewrites what the rest of these tools report
on, so exposing either would let the thing being granted a right be the thing
that grants it. These stay human-at-a-terminal operations.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastmcp", reason="requires the 'mcp' extra")

from lionagi.mcp import server, surface  # noqa: E402 — must follow the extra guard

# Named by the operation an agent could otherwise perform on itself, not by a
# spelling: a rename that keeps the capability must still fail this.
FENCED = ("plugin_trust", "hooks_trust", "state_migrate")


def test_no_privilege_granting_tool_is_registered():
    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert registered.isdisjoint(FENCED), sorted(registered.intersection(FENCED))


def test_no_privilege_granting_callable_is_exported_for_registration():
    # The registry is what `register()` walks, so a function that exists but is
    # unlisted is already unreachable; catching it here keeps a later "add it
    # back to TOOLS" from being a one-line change nobody reviews.
    assert {fn.__name__ for fn in surface.TOOLS}.isdisjoint(FENCED)
