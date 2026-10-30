# Copyright (c) 2023-2026, LionAGI contributors
# SPDX-License-Identifier: Apache-2.0

"""Audit and warning behavior for Session authorization denials."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import warnings

import pytest
import pytest_asyncio

from lionagi.session.control import ToolInvocation
from lionagi.session.observer import SessionObserver
from lionagi.session.signal import GateDenied, Signal
from lionagi.state.db import StateDB


@pytest_asyncio.fixture
async def audit_store(tmp_path):
    path = tmp_path / "state.db"
    db = StateDB(path)
    await db.open()
    await db.create_progression("audit-progression")
    await db.create_session(
        {
            "id": "audit-session",
            "created_at": 100.0,
            "progression_id": "audit-progression",
            "name": "audit",
            "status": "running",
            "invocation_kind": "agent",
        }
    )
    try:
        yield path, db
    finally:
        await db.close()


def _rows(path):
    with sqlite3.connect(str(path)) as conn:
        return conn.execute("SELECT kind, payload FROM session_signals ORDER BY seq").fetchall()


@pytest.mark.parametrize("case", ["absent", "allow", "deny", "raise", "invalid"])
async def test_private_gate_evaluation_has_no_observation_side_effects(case, audit_store):
    path, db = audit_store
    observer = SessionObserver()
    observer.bind_db_persistence("audit-session", db=db)
    seen = []
    observer.route(lambda payload: seen.append(payload) or True, into="audit")
    observer.observe(GateDenied, handler=lambda event, _ctx: seen.append(event))
    action = ToolInvocation(function="tool", arguments={"value": "private-value"})
    calls = []

    def gate(value):
        calls.append(value)
        if case == "raise":
            raise RuntimeError("private-value")
        if case == "invalid":
            return "invalid"
        return case == "allow"

    if case != "absent":
        observer.gate(gate)
    result = await observer._evaluate_gate(action=action)
    assert result.allowed is (case in {"absent", "allow"})
    assert result.errored is (case in {"raise", "invalid"})
    assert calls == ([] if case == "absent" else [action])
    assert not list(observer.flow.items)
    assert not seen
    assert _rows(path) == []


async def test_private_gate_evaluation_propagates_cancellation(audit_store):
    path, db = audit_store
    observer = SessionObserver()
    observer.bind_db_persistence("audit-session", db=db)

    def gate(_action):
        raise asyncio.CancelledError()

    observer.gate(gate)
    with pytest.raises(asyncio.CancelledError):
        await observer._evaluate_gate(action=object())
    assert not list(observer.flow.items)
    assert _rows(path) == []


@pytest.mark.parametrize("case", ["absent", "deny", "raise"])
async def test_authorization_audits_only_redacted_denials(case, audit_store):
    path, db = audit_store
    observer = SessionObserver()
    observer.bind_db_persistence("audit-session", db=db)
    secret = "private-value-to-never-store"
    action = ToolInvocation(function="tool", arguments={"value": secret})
    calls = []

    def gate(value):
        calls.append(value)
        if case == "raise":
            raise RuntimeError(secret)
        return False

    if case != "absent":
        observer.gate(gate)
    assert await observer.authorize(action) is (case == "absent")
    assert calls == ([] if case == "absent" else [action])
    rows = _rows(path)
    assert len(rows) == (0 if case == "absent" else 1)
    if rows:
        assert rows[0][0] == "GateDenied"
        assert secret not in rows[0][1]
        assert "data" not in json.loads(rows[0][1])
        flow = observer.by_type(GateDenied)
        assert len(flow) == 1 and flow[0].data is None
    else:
        assert not observer.by_type(GateDenied)


@pytest.mark.parametrize("failure", ["subscriber", "route"])
async def test_failing_observer_cannot_drop_denial_audit(failure, audit_store):
    path, db = audit_store
    observer = SessionObserver()

    def broken(*_args):
        raise RuntimeError("observer failed")

    if failure == "subscriber":
        # Register before the sink; old emit order runs this first.
        observer.observe(GateDenied, handler=broken)
    else:
        observer.route(broken, into="audit")
    observer.bind_db_persistence("audit-session", db=db)
    observer.gate(lambda _action: False)
    assert await observer.authorize(ToolInvocation(function="tool", arguments={})) is False
    assert [kind for kind, _payload in _rows(path)] == ["GateDenied"]
    assert len(observer.by_type(GateDenied)) == 1


async def test_denial_is_persisted_once_per_binding(audit_store):
    path, db = audit_store
    observer = SessionObserver()
    observer.gate(lambda _action: False)
    action = ToolInvocation(function="tool", arguments={})
    observer.bind_db_persistence("audit-session", db=db)
    assert await observer.authorize(action) is False
    observer.unbind_db_persistence()
    assert await observer.authorize(action) is False
    observer.bind_db_persistence("audit-session", db=db)
    assert await observer.authorize(action) is False
    assert [kind for kind, _payload in _rows(path)] == ["GateDenied", "GateDenied"]
    assert len(observer.by_type(GateDenied)) == 3


async def test_rebinding_while_denial_is_persisting_does_not_duplicate():
    started, release = asyncio.Event(), asyncio.Event()

    class Store:
        def __init__(self, *, hold=False):
            self.rows = []
            self.hold = hold

        async def insert_session_signal(self, **row):
            if self.hold:
                started.set()
                await release.wait()
            self.rows.append(row)

    old, new = Store(hold=True), Store()
    observer = SessionObserver()
    observer.bind_db_persistence("audit-session", db=old)
    observer.gate(lambda _action: False)
    task = asyncio.create_task(observer.authorize(object()))
    await asyncio.wait_for(started.wait(), timeout=5)
    observer.bind_db_persistence("audit-session", db=new)
    release.set()
    assert await asyncio.wait_for(task, timeout=5) is False
    assert [row["kind"] for row in old.rows] == ["GateDenied"]
    assert new.rows == []
    assert len(observer.by_type(GateDenied)) == 1
    assert await observer.authorize(object()) is False
    assert [row["kind"] for row in new.rows] == ["GateDenied"]


async def test_cancelled_audit_propagates_cancellation():
    started = asyncio.Event()

    class Store:
        async def insert_session_signal(self, **_row):
            started.set()
            await asyncio.Event().wait()

    observer = SessionObserver()
    observer.bind_db_persistence("audit-session", db=Store())
    observer.gate(lambda _action: False)
    task = asyncio.create_task(observer.authorize(object()))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


async def test_async_subscriber_then_failing_sync_subscriber_closes_coroutine(audit_store):
    path, db = audit_store
    observer = SessionObserver()
    created = []

    async def body():
        pass

    def first(_event, _ctx):
        result = body()
        created.append(result)
        return result

    def broken(_event, _ctx):
        raise RuntimeError("observer failed")

    observer.observe(GateDenied, handler=first)
    observer.observe(GateDenied, handler=broken)
    observer.bind_db_persistence("audit-session", db=db)
    observer.gate(lambda _action: False)
    try:
        assert await observer.authorize(object()) is False
        assert [kind for kind, _payload in _rows(path)] == ["GateDenied"]
        assert len(created) == 1
        assert inspect.getcoroutinestate(created[0]) == inspect.CORO_CLOSED
    finally:
        for coro in created:
            coro.close()


async def test_emit_gate_suppression_warns_but_authorize_alone_does_not():
    observer = SessionObserver()
    observer.gate(lambda _action: False)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        assert await observer.authorize(object()) is False
    assert not [w for w in captured if issubclass(w.category, DeprecationWarning)]
    with pytest.warns(DeprecationWarning, match="gate"):
        assert await observer.emit(Signal(data="suppressed")) == []
