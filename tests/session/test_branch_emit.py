# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Branch.emit closes the loop: a branch raises a typed event into its
session's reactive observer; a standalone branch no-ops.
"""

from __future__ import annotations

from lionagi.protocols.generic.event import Event
from lionagi.session.branch import Branch
from lionagi.session.session import Session


class Noticed(Event):
    note: str = ""


async def test_included_branch_emits_to_session_observer():
    s = Session()
    seen = []

    @s.observe(Noticed)
    async def on_notice(event, session):
        # handler receives the bound session, not the branch
        assert session is s
        seen.append(event.note)
        return "ok"

    branch = s.default_branch
    assert branch._observer is s.observer

    results = await branch.emit(Noticed(note="inside"))
    assert results == ["ok"]
    assert seen == ["inside"]


async def test_new_branch_gets_observer():
    s = Session()
    fired = []

    @s.observe(Noticed)
    def on_notice(event, session):
        fired.append(event.note)

    b = s.new_branch()
    assert b._observer is s.observer
    await b.emit(Noticed(note="x"))
    assert fired == ["x"]


async def test_standalone_branch_emit_is_noop():
    b = Branch()
    assert await b.emit(Noticed(note="nowhere")) == []


async def test_branch_emit_respects_gate():
    s = Session()
    fired = []

    @s.observe(Noticed)
    def on_notice(event, session):
        fired.append(event.note)

    s.gate(lambda e: "allow" in e.note)

    await s.default_branch.emit(Noticed(note="allow this"))
    await s.default_branch.emit(Noticed(note="deny this"))

    assert fired == ["allow this"]
    assert len(s.observer.by_type(Noticed)) == 2  # both recorded
