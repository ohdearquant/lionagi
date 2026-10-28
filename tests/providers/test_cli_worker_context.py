# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest


def test_none_masks_an_owner_and_restores_it_after_an_error():
    from lionagi._cli_worker_context import (
        _bind_cli_worker_recorder,
        _get_cli_worker_recorder,
    )

    outer = object()
    original = _get_cli_worker_recorder()
    with _bind_cli_worker_recorder(outer):
        assert _get_cli_worker_recorder() is outer
        with pytest.raises(RuntimeError, match="inner scope"):
            with _bind_cli_worker_recorder(None):
                assert _get_cli_worker_recorder() is None
                raise RuntimeError("inner scope")
        assert _get_cli_worker_recorder() is outer
    assert _get_cli_worker_recorder() is original


@pytest.mark.asyncio(loop_scope="function")
async def test_a_child_keeps_the_owner_bound_when_its_task_was_created():
    from lionagi._cli_worker_context import (
        _bind_cli_worker_recorder,
        _get_cli_worker_recorder,
    )

    first, second = object(), object()
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def child():
        entered.set()
        await resume.wait()
        return _get_cli_worker_recorder()

    task = None
    try:
        with _bind_cli_worker_recorder(first):
            task = asyncio.create_task(child())
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            with _bind_cli_worker_recorder(second):
                resume.set()
                observed = await asyncio.wait_for(task, timeout=1.0)
                assert observed is first, "a descendant must retain its admitted owner"
                assert _get_cli_worker_recorder() is second
            assert _get_cli_worker_recorder() is first
    finally:
        resume.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio(loop_scope="function")
async def test_an_explicitly_unpersisted_child_does_not_inherit_an_outer_owner():
    from lionagi._cli_worker_context import (
        _bind_cli_worker_recorder,
        _get_cli_worker_recorder,
    )

    outer = object()

    async def child():
        return _get_cli_worker_recorder()

    with _bind_cli_worker_recorder(outer):
        with _bind_cli_worker_recorder(None):
            task = asyncio.create_task(child())
        try:
            observed = await asyncio.wait_for(task, timeout=1.0)
            assert observed is None, "an unpersisted child must not borrow an outer owner"
            assert _get_cli_worker_recorder() is outer
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
