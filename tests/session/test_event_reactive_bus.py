# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for branch.emit_and_log and the session observer filter DSL (type + status + field)."""

from __future__ import annotations

import pytest

from lionagi.protocols.generic.event import Event, EventStatus
from lionagi.session.branch import Branch
from lionagi.session.session import Session


class Call(Event):
    """A stand-in event (no network) to exercise the bus + filter DSL."""


def _at(status: EventStatus, *, duration: float | None = None) -> Call:
    ev = Call()
    ev.execution.status = status
    if duration is not None:
        ev.execution.duration = duration
    return ev


@pytest.mark.asyncio
async def test_emit_and_log_reacts_to_failed_status():
    """observe(EventType, EventStatus.FAILED) fires on a failed event, and the
    event is also logged (durable record)."""
    s = Session()
    branch = s.default_branch
    failed: list[Call] = []
    s.observe(Call, EventStatus.FAILED, handler=lambda e, _: failed.append(e))

    ok = _at(EventStatus.COMPLETED)
    bad = _at(EventStatus.FAILED)
    await branch.emit_and_log(ok)
    await branch.emit_and_log(bad)

    assert failed == [bad]
    assert len(branch._log_manager.logs) == 2


@pytest.mark.asyncio
async def test_status_enum_alone_is_a_filter():
    """A bare EventStatus member is a filter over any event's status."""
    s = Session()
    completed: list[Call] = []
    s.observe(EventStatus.COMPLETED, handler=lambda e, _: completed.append(e))

    ok = _at(EventStatus.COMPLETED)
    bad = _at(EventStatus.FAILED)
    await s.default_branch.emit_and_log(ok)
    await s.default_branch.emit_and_log(bad)

    assert completed == [ok]


@pytest.mark.asyncio
async def test_compositional_type_status_field():
    """observe(Type, EventStatus.COMPLETED, Type.q.duration > N) —
    three-way composition: only a completed AND slow event reacts."""
    s = Session()
    slow_ok: list[Call] = []
    s.observe(
        Call,
        EventStatus.COMPLETED,
        Call.q.duration > 1.0,
        handler=lambda e, _: slow_ok.append(e),
    )

    fast = _at(EventStatus.COMPLETED, duration=0.1)
    slow = _at(EventStatus.COMPLETED, duration=5.0)
    slow_but_failed = _at(EventStatus.FAILED, duration=9.0)
    for ev in (fast, slow, slow_but_failed):
        await s.default_branch.emit_and_log(ev)

    assert slow_ok == [slow]  # completed AND duration>1.0


@pytest.mark.asyncio
async def test_field_handle_on_execution_duration():
    """Event.q.duration resolves the nested execution.duration field."""
    s = Session()
    seen: list[Call] = []
    s.observe(Call.q.duration > 3600, handler=lambda e, _: seen.append(e))

    await s.default_branch.emit_and_log(_at(EventStatus.COMPLETED, duration=10.0))
    await s.default_branch.emit_and_log(_at(EventStatus.COMPLETED, duration=7200.0))

    assert [e.execution.duration for e in seen] == [7200.0]


@pytest.mark.asyncio
async def test_emit_and_log_standalone_branch_only_logs():
    """A standalone branch (no session/observer) logs but does not emit — no error."""
    b = Branch()
    assert b._observer is None
    res = await b.emit_and_log(_at(EventStatus.FAILED))
    assert res == []  # no observer → no handlers
    assert len(b._log_manager.logs) == 1  # still logged
