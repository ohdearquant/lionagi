# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Characterize the reusable wait loop without opening a database."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace

import anyio
import pytest


class _MemoryContext:
    def __init__(self):
        self.entered = 0
        self.exits = []

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exits.append((exc_type, exc))
        return False


@pytest.fixture
def polling(monkeypatch):
    from lionagi.cli import wait
    from lionagi.state import db

    context = _MemoryContext()
    monkeypatch.setattr(db, "StateDB", lambda: context)

    async def resolve(_db, run_id):
        assert _db is context
        return "session", {"id": run_id, "status": "running"}

    async def outcome(_db, kind, row):
        assert _db is context
        assert kind == "session"
        return {"run_id": row["id"], "status": row["status"]}

    monkeypatch.setattr(wait, "_resolve_wait_target", resolve)
    monkeypatch.setattr(wait, "_build_outcome", outcome)
    return SimpleNamespace(core=wait, context=context)


@pytest.mark.asyncio
async def test_pending_poll_yields_before_refetching_again(polling, monkeypatch):
    yielded = asyncio.Event()
    refetches = []

    async def refetch(db, kind, run_id):
        assert db is polling.context
        assert kind == "session"
        refetches.append(run_id)
        if len(refetches) == 1:
            asyncio.get_running_loop().call_soon(yielded.set)
            return {"id": run_id, "status": "running"}
        assert yielded.is_set()
        return {"id": run_id, "status": "completed"}

    monkeypatch.setattr(polling.core, "_refetch", refetch)
    callbacks = []
    with anyio.fail_after(2):
        results = await polling.core.wait_for_terminal(
            ["first"], interval=0, on_result=callbacks.append
        )

    assert refetches == ["first", "first"]
    assert results == [{"run_id": "first", "status": "completed"}]
    assert callbacks == results
    assert polling.context.entered == 1
    assert polling.context.exits == [(None, None)]


@pytest.mark.asyncio
async def test_callbacks_follow_completion_but_results_follow_input_order(polling, monkeypatch):
    refetches = []

    async def refetch(_db, _kind, run_id):
        refetches.append(run_id)
        status = "running" if refetches == ["first"] else "completed"
        return {"id": run_id, "status": status}

    monkeypatch.setattr(polling.core, "_refetch", refetch)
    callbacks = []
    with anyio.fail_after(2):
        results = await polling.core.wait_for_terminal(
            ["first", "second"], interval=0, on_result=callbacks.append
        )

    assert refetches == ["first", "second", "first"]
    assert [row["run_id"] for row in callbacks] == ["second", "first"]
    assert [row["run_id"] for row in results] == ["first", "second"]
    assert polling.context.exits == [(None, None)]


@pytest.mark.asyncio
async def test_disappearing_pending_row_reports_unknown_once(polling, monkeypatch):
    async def refetch(_db, _kind, _run_id):
        return None

    monkeypatch.setattr(polling.core, "_refetch", refetch)
    callbacks = []
    with anyio.fail_after(2):
        results = await polling.core.wait_for_terminal(["gone"], on_result=callbacks.append)

    assert results == [
        {
            "run_id": "gone",
            "kind": "session",
            "status": "unknown",
            "reason": "unknown",
            "artifact_dir": None,
            "exit_code": None,
            "success": False,
        }
    ]
    assert callbacks == results
    assert polling.context.exits == [(None, None)]


@pytest.mark.asyncio
async def test_cancellation_while_polling_leaves_context(polling, monkeypatch):
    refetched = asyncio.Event()

    async def refetch(_db, _kind, run_id):
        refetched.set()
        return {"id": run_id, "status": "running"}

    monkeypatch.setattr(polling.core, "_refetch", refetch)
    callbacks = []
    task = asyncio.create_task(
        polling.core.wait_for_terminal(["running"], interval=3600, on_result=callbacks.append)
    )
    try:
        with anyio.fail_after(2):
            await refetched.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert task.cancelled()
        assert callbacks == []
        assert len(polling.context.exits) == 1
        exc_type, exc = polling.context.exits[0]
        assert exc_type is asyncio.CancelledError
        assert isinstance(exc, asyncio.CancelledError)
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_callback_failure_propagates_and_leaves_context(polling, monkeypatch):
    async def refetch(_db, _kind, run_id):
        return {"id": run_id, "status": "completed"}

    monkeypatch.setattr(polling.core, "_refetch", refetch)
    failure = RuntimeError("callback failed")
    callbacks = []

    def on_result(outcome):
        callbacks.append(outcome)
        raise failure

    with anyio.fail_after(2):
        with pytest.raises(RuntimeError) as raised:
            await polling.core.wait_for_terminal(["first"], on_result=on_result)

    assert raised.value is failure
    assert callbacks == [{"run_id": "first", "status": "completed"}]
    assert polling.context.exits == [(RuntimeError, failure)]
